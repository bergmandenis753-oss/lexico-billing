import tempfile
import unittest
from pathlib import Path

import db
import did_module


class DidBillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        did_module.init_schema(db)
        conn = db.get_conn()
        client = conn.execute(
            """INSERT INTO did_clients
                   (name, balance_cents, credit_limit_cents, outbound_ips, outbound_tech_prefix)
               VALUES ('Inbound test', 100000, 0, '198.51.100.0/24', '500')"""
        )
        self.client_id = client.lastrowid
        route = conn.execute(
            """INSERT INTO did_numbers
                   (client_id, did_number, provider_name, provider_ips, destination,
                    sell_rate_cents, cost_rate_cents, billing_cycle, max_channels)
               VALUES (?, '48221234567', 'Provider', '203.0.113.0/24',
                       '198.51.100.20:5060', 1000, 400, '1/1', 2)""",
            (self.client_id,),
        )
        self.number_id = route.lastrowid
        self.outbound_route_id = conn.execute(
            """INSERT INTO did_outbound_routes
                   (client_id, provider_name, destination_name, prefix, route_ips,
                    tech_prefix, sell_rate_cents, cost_rate_cents, billing_cycle, max_channels)
               VALUES (?, 'OneClick', 'Poland', '48', '91.224.250.26',
                       '99901', 900, 325, '1/1', 4)""",
            (self.client_id,),
        ).lastrowid
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tmp.cleanup()

    def test_reserve_finalize_and_idempotency(self):
        reserved = did_module.reserve_call(db, did_module.DidReserveIn(
            did_number="+48 22 123 45 67",
            caller_id="49123456789",
            source_ip="203.0.113.45",
            call_uuid="did-call-1",
        ))
        self.assertTrue(reserved["allowed"])
        self.assertEqual(reserved["bridge_target"], "sofia/external/48221234567@198.51.100.20:5060")

        result = did_module.finalize_call(db, did_module.DidFinalizeIn(
            call_uuid="did-call-1", billsec=61, hangup_cause="NORMAL_CLEARING", result="Normal"
        ))
        self.assertEqual(result["charged_cents"], 1017)
        self.assertEqual(result["cost_cents"], 407)
        self.assertEqual(result["balance_cents"], 98983)

        repeated = did_module.finalize_call(db, did_module.DidFinalizeIn(
            call_uuid="did-call-1", billsec=600, result="Normal"
        ))
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["charged_cents"], 1017)
        self.assertEqual(repeated["balance_cents"], 98983)

    def test_provider_whitelist_and_channel_limit(self):
        with self.assertRaisesRegex(Exception, "whitelist"):
            did_module.reserve_call(db, did_module.DidReserveIn(
                did_number="48221234567", source_ip="192.0.2.1", call_uuid="blocked"
            ))

        for index in range(2):
            did_module.reserve_call(db, did_module.DidReserveIn(
                did_number="48221234567", source_ip="203.0.113.10", call_uuid=f"active-{index}"
            ))
        with self.assertRaisesRegex(Exception, "Лимит"):
            did_module.reserve_call(db, did_module.DidReserveIn(
                did_number="48221234567", source_ip="203.0.113.10", call_uuid="active-3"
            ))

    def test_outbound_caller_id_whitelist_and_billing(self):
        with self.assertRaisesRegex(Exception, "Caller ID"):
            did_module.reserve_outbound_call(db, did_module.DidOutboundReserveIn(
                source_ip="198.51.100.10", destination="50048606123456",
                caller_id="48229999999", call_uuid="wrong-a-number",
            ))

        reserved = did_module.reserve_outbound_call(db, did_module.DidOutboundReserveIn(
            source_ip="198.51.100.10", destination="50048606123456",
            caller_id="48221234567", call_uuid="outbound-1",
        ))
        self.assertEqual(reserved["dial_destination"], "48606123456")
        self.assertEqual(reserved["provider_number"], "9990148606123456")
        self.assertEqual(reserved["bridge_target"], "sofia/external/9990148606123456@91.224.250.26")
        self.assertEqual(reserved["caller_id"], "48221234567")

        result = did_module.finalize_outbound_call(db, did_module.DidOutboundFinalizeIn(
            call_uuid="outbound-1", billsec=60, hangup_cause="NORMAL_CLEARING", result="Normal"
        ))
        self.assertEqual(result["charged_cents"], 900)
        self.assertEqual(result["cost_cents"], 325)
        self.assertEqual(result["balance_cents"], 99100)

        repeated = did_module.finalize_outbound_call(db, did_module.DidOutboundFinalizeIn(
            call_uuid="outbound-1", billsec=600, result="Normal"
        ))
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["balance_cents"], 99100)

    def test_outbound_route_reuses_live_terminator_settings(self):
        conn = db.get_conn()
        conn.execute("UPDATE did_outbound_routes SET active = 0")
        group_id = conn.execute(
            "INSERT INTO termination_groups (name, ips, gateway_name) VALUES ('Existing OneClick', '91.224.250.26', '')"
        ).lastrowid
        terminator_id = conn.execute(
            """INSERT INTO terminators
                   (name, gateway_group_id, destination_name, prefix, gateway_name,
                    tech_prefix, cost_rate_cents, billing_cycle, active)
               VALUES ('OneClick PL', ?, 'Poland', '48', '', '777', 300, '60/1', 1)""",
            (group_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO did_outbound_routes
                   (client_id, terminator_id, provider_name, destination_name, prefix,
                    sell_rate_cents, cost_rate_cents, billing_cycle, max_channels)
               VALUES (?, ?, 'stale', 'stale', '1', 900, 1, '1/1', 4)""",
            (self.client_id, terminator_id),
        )
        conn.commit()
        conn.close()

        first = did_module.reserve_outbound_call(db, did_module.DidOutboundReserveIn(
            source_ip="198.51.100.10", destination="50048606123456",
            caller_id="48221234567", call_uuid="linked-1",
        ))
        self.assertEqual(first["provider_name"], "Existing OneClick")
        self.assertEqual(first["provider_number"], "77748606123456")
        self.assertEqual(first["bridge_target"], "sofia/external/77748606123456@91.224.250.26")
        self.assertEqual(first["cost_rate_cents"], 300)
        self.assertEqual(first["cost_billing_cycle"], "60/1")

        conn = db.get_conn()
        conn.execute("UPDATE did_outbound_reservations SET active = 0")
        conn.execute("UPDATE termination_groups SET ips = '91.224.250.27' WHERE id = ?", (group_id,))
        conn.execute(
            "UPDATE terminators SET tech_prefix = '778', cost_rate_cents = 350 WHERE id = ?",
            (terminator_id,),
        )
        conn.commit()
        conn.close()

        second = did_module.reserve_outbound_call(db, did_module.DidOutboundReserveIn(
            source_ip="198.51.100.10", destination="50048606123456",
            caller_id="48221234567", call_uuid="linked-2",
        ))
        self.assertEqual(second["provider_number"], "77848606123456")
        self.assertEqual(second["bridge_target"], "sofia/external/77848606123456@91.224.250.27")
        self.assertEqual(second["cost_rate_cents"], 350)


if __name__ == "__main__":
    unittest.main()
