import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "sip_crm_edge" / "sync_routes.py"
SPEC = importlib.util.spec_from_file_location("sip_crm_sync_routes", MODULE_PATH)
sync_routes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_routes)


class SipCrmEdgeTests(unittest.TestCase):
    def test_render_uses_only_local_exact_routes(self):
        xml = sync_routes.render_dialplan({
            "routes": [{
                "inbound_number": "+777 800 326 50",
                "originator_ips": ["195.219.39.9", "195.219.39.14"],
                "bridge_target": "sofia/external/sip:hash@kz.pbx.de.backend.salesrender.com",
            }]
        })
        self.assertIn('expression="^\\+?77780032650$"', xml)
        self.assertIn('field="network_addr"', xml)
        self.assertIn(r"195\.219\.39\.9", xml)
        self.assertIn("sofia/external/sip:hash@kz.pbx.de.backend.salesrender.com", xml)
        self.assertNotIn("http", xml)

    def test_invalid_update_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "routes.xml"
            output.write_text("last-known-good", encoding="utf-8")
            with self.assertRaises(ValueError):
                sync_routes.render_dialplan({
                    "routes": [{
                        "inbound_number": "77780032650",
                        "originator_ips": ["195.219.39.9"],
                        "bridge_target": "https://bad",
                    }]
                })
            self.assertEqual(output.read_text(encoding="utf-8"), "last-known-good")

    def test_route_without_originator_ip_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "originator IPs"):
            sync_routes.render_dialplan({
                "routes": [{
                    "inbound_number": "77780032650",
                    "originator_ips": [],
                    "bridge_target": "sofia/external/sip:hash@example.com",
                }]
            })

    def test_atomic_update_reloads_only_when_content_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "routes.xml"
            with mock.patch.object(sync_routes.subprocess, "run") as run:
                self.assertTrue(sync_routes.atomic_update(output, "first"))
                self.assertFalse(sync_routes.atomic_update(output, "first"))
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
