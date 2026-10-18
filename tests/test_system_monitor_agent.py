import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "freeswitch" / "system_monitor_agent.py"
SPEC = importlib.util.spec_from_file_location("system_monitor_agent", MODULE_PATH)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class SystemMonitorAgentTests(unittest.TestCase):
    def test_cpu_percent_includes_steal_and_reports_it_separately(self):
        usage, steal = agent.cpu_percent((1000, 500, 40), (1200, 560, 60))
        self.assertEqual(usage, 70.0)
        self.assertEqual(steal, 10.0)

    def test_quality_uses_loss_rate_and_jitter_standard_deviation(self):
        accumulator = agent.QualityAccumulator()
        accumulator.add_event({
            "variable_rtp_audio_in_packet_count": "400",
            "variable_rtp_audio_in_jitter_loss_rate": "0.025",
            "variable_rtp_audio_in_jitter_max_variance": "225",
        })

        result = accumulator.take()

        self.assertEqual(result["packet_loss_percent"], 2.5)
        self.assertEqual(result["jitter_ms"], 15.0)
        self.assertEqual(result["rtp_packets"], 400)
        self.assertEqual(result["rtp_legs"], 1)

    def test_voice_node_is_read_from_freeswitch_node_file(self):
        with tempfile.TemporaryDirectory() as directory:
            node_file = Path(directory) / "voice_node"
            node_file.write_text("secondary\n", encoding="utf-8")

            self.assertEqual(agent.configured_voice_node(node_file), "secondary")

    def test_incident_trigger_detects_bad_rtp_quality(self):
        payload = {
            "cpu_percent": 42,
            "cpu_steal_percent": 1,
            "packet_loss_percent": 6.25,
            "jitter_ms": 55,
            "rtp_packets": 1000,
        }

        reasons = agent.incident_reasons(payload)

        self.assertEqual(len(reasons), 2)
        self.assertIn("RTP packet loss", reasons[0])
        self.assertIn("RTP jitter", reasons[1])
        self.assertIn("сетевая перегрузка", agent.suspected_cause(payload, reasons))

    def test_incident_trigger_ignores_quality_without_rtp_samples(self):
        payload = {
            "cpu_percent": 20,
            "cpu_steal_percent": 0,
            "packet_loss_percent": 99,
            "jitter_ms": 500,
            "rtp_packets": 0,
        }

        self.assertEqual(agent.incident_reasons(payload), [])

    def test_incident_trigger_identifies_cpu_overload(self):
        payload = {
            "cpu_percent": 96,
            "cpu_steal_percent": 2,
            "rtp_packets": 0,
        }

        reasons = agent.incident_reasons(payload)

        self.assertEqual(reasons, ["CPU 96.0%"])
        self.assertIn("локальная перегрузка", agent.suspected_cause(payload, reasons))

    def test_vendor_leg_matching_uses_outbound_gateway_or_ip(self):
        values = {
            "Call-Direction": "outbound",
            "variable_sip_network_ip": "91.22.4.250",
            "variable_remote_media_ip": "91.22.4.251",
            "variable_sip_gateway_name": "one-gw",
        }

        self.assertTrue(
            agent._channel_matches_vendor(values, ["91.22.4.250"], ["one-gw"])
        )
        self.assertEqual(
            agent._vendor_addresses(values, ["91.22.4.250"]),
            ["91.22.4.250", "91.22.4.251"],
        )
        self.assertFalse(
            agent._channel_matches_vendor(
                {**values, "Call-Direction": "inbound"},
                ["91.22.4.250"],
                ["one-gw"],
            )
        )

    def test_automatic_incident_capture_is_disabled_by_default(self):
        self.assertFalse(agent.AUTOMATIC_INCIDENTS_ENABLED)


if __name__ == "__main__":
    unittest.main()
