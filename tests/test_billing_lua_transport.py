import unittest
from pathlib import Path


class BillingLuaTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (
            Path(__file__).resolve().parents[1] / "freeswitch" / "billing.lua"
        ).read_text(encoding="utf-8")

    def test_uses_native_mod_curl_instead_of_shell_processes(self):
        self.assertIn('freeswitch.API():execute("curl", command)', self.script)
        self.assertNotIn("io.popen", self.script)
        self.assertNotIn("curl -s", self.script)

    def test_native_requests_have_bounded_timeouts(self):
        self.assertIn('"connect-timeout", "2"', self.script)
        self.assertIn('"timeout", "4"', self.script)

    def test_disconnected_call_is_not_bridged(self):
        self.assertIn("if session:ready() then", self.script)
        self.assertIn("caller disconnected before bridge", self.script)


if __name__ == "__main__":
    unittest.main()
