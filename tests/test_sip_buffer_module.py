import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import sip_buffer_module


class _NoAuth:
    ADMIN_AUTH = []
    ADMIN_WRITE_AUTH = []
    API_AUTH = []


class _BillingDb:
    DB_PATH = Path("unused-billing.db")


class SipBufferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.buffer_path = Path(self.tmp.name) / "sip_buffer.db"
        self.billing_path = Path(self.tmp.name) / "billing.db"
        self.billing_path.write_text("billing-sentinel", encoding="utf-8")
        _BillingDb.DB_PATH = self.billing_path
        self.env = mock.patch.dict(os.environ, {"SIP_BUFFER_DB_PATH": str(self.buffer_path)})
        self.env.start()
        app = FastAPI()
        sip_buffer_module.install(app, _NoAuth, _BillingDb, Path(sip_buffer_module.__file__).parent)
        self.http = TestClient(app)

    def tearDown(self):
        self.http.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_separate_database_and_direct_route_export(self):
        node = self.http.post("/api/sip-buffer/nodes", json={
            "name": "SalesRender Buffer",
            "node_key": "salesrender-edge-1",
            "public_ip": "165.245.208.124",
            "active": True,
        })
        self.assertEqual(node.status_code, 200, node.text)
        route = self.http.post("/api/sip-buffer/routes", json={
            "node_id": node.json()["id"],
            "name": "Lexico to SalesRender KZ",
            "originator_name": "Lexico",
            "originator_ips": "195.219.39.9\n195.219.39.14",
            "inbound_number": "77780032650",
            "terminator_name": "SalesRender",
            "destination_uri": "sip:{1ec1c7b49dd8d2378e31407c195a0fe2}:{kz.pbx.de.backend.salesrender.com}",
            "active": True,
        })
        self.assertEqual(route.status_code, 200, route.text)

        exported = self.http.get("/api/freeswitch/sip-crm-config/salesrender-edge-1")
        self.assertEqual(exported.status_code, 200, exported.text)
        payload = exported.json()
        self.assertEqual(payload["database"], "isolated")
        self.assertEqual(payload["routes"][0]["inbound_number"], "77780032650")
        self.assertEqual(payload["routes"][0]["originator_ips"], ["195.219.39.9", "195.219.39.14"])
        self.assertEqual(
            payload["routes"][0]["destination_uri"],
            "sip:1ec1c7b49dd8d2378e31407c195a0fe2@kz.pbx.de.backend.salesrender.com",
        )
        self.assertEqual(self.billing_path.read_text(encoding="utf-8"), "billing-sentinel")
        self.assertTrue(self.buffer_path.exists())

    def test_ui_has_no_did_dependency(self):
        page = self.http.get("/sip-crm")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Отдельный SIP-буфер", page.text)
        self.assertIn("/api/sip-buffer", page.text)
        self.assertNotIn("/api/dids/sip-targets", page.text)

    def test_invalid_originator_ip_is_rejected(self):
        conn = sqlite3.connect(self.buffer_path)
        node_id = conn.execute(
            "INSERT INTO sip_buffer_nodes(name,node_key,active) VALUES('Buffer','buffer-1',1)"
        ).lastrowid
        conn.commit()
        conn.close()
        response = self.http.post("/api/sip-buffer/routes", json={
            "node_id": node_id,
            "name": "bad",
            "originator_name": "Lexico",
            "originator_ips": "not-an-ip",
            "inbound_number": "77780032650",
            "terminator_name": "SalesRender",
            "destination_uri": "sip:hash@example.com",
            "active": True,
        })
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
