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

    def test_reserve_503_is_returned_to_the_caller(self):
        self.assertIn('tonumber(sip_code) == 503 and "503" or "403"', self.script)
        self.assertIn('reserve rejected (" .. code .. "): " .. body, code)', self.script)

    def test_audio_ivr_checks_local_wav_before_answering(self):
        self.assertIn('local AUDIO_DIR = "/usr/share/freeswitch/sounds/lexico/ivr/"', self.script)
        self.assertIn('if not file_exists(audio_path) then', self.script)
        self.assertLess(self.script.index('if not file_exists(audio_path) then'), self.script.index('session:answer()'))
        self.assertIn('result = "AUDIO_IVR"', self.script)


if __name__ == "__main__":
    unittest.main()
