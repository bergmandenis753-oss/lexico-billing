import importlib.util
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


if __name__ == "__main__":
    unittest.main()
