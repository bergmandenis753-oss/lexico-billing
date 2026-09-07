import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import caller_id
import db
import main
import credit_limit_calls
from fastapi import FastAPI
from fastapi.testclient import TestClient


class CallerIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "BILLING_DB_PATH": str(Path(self.tmp.name) / "test.db"),
            "ADMIN_USER": "test", "ADMIN_PASSWORD": "test-password",
            "API_SECRET_KEY": "test-api-key",
        })
        env.start()
        self.addCleanup(env.stop)
        database = patch.object(db, 'DB_PATH', Path(self.tmp.name) / 'test.db')
        database.start()
        self.addCleanup(database.stop)
        db.init_db()
        with db.get_conn() as conn:
            conn.execute("INSERT INTO clients (id, name, sip_ip, balance_cents) VALUES (1, 'Test', '192.0.2.1', 100000)")
            conn.execute("INSERT INTO terminators (id, name, destination_name, prefix, gateway_name, cost_rate_cents) VALUES (1, 'Test provider', 'Poland', '48', 'test-gateway', 1000)")
            conn.execute("INSERT INTO client_rates (id, client_id, terminator_id, prefix, destination_name, sell_rate_cents) VALUES (1, 1, 1, '48', 'Poland', 2000)")
            conn.execute("INSERT INTO client_rates (id, client_id, terminator_id, prefix, destination_name, sell_rate_cents) VALUES (2, 1, 1, '49', 'Germany', 3000)")
        app = FastAPI()
        app.include_router(main.app.router)
        app.router.routes = [r for r in app.router.routes if getattr(r, 'path', '') not in {'/api/reserve', '/api/finalize'}]
        credit_limit_calls.install_call_routes(app, main, db)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def save(self, value, rid=1):
        return self.http.put(f"/api/client-rates/{rid}/caller-id", json={"numbers": value},
                             auth=("test", "test-password"), headers={"X-Money-Scale": str(db.MONEY_SCALE)})

    def choose(self, uuid, rid=1):
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rate = conn.execute("SELECT * FROM client_rates WHERE id = ?", (rid,)).fetchone()
            number = caller_id.select_number(conn, rate, uuid, 1)
            conn.execute("INSERT OR REPLACE INTO reservations (client_id, call_uuid, reserved_cents, expires_at, outbound_caller_id, caller_id_rate_id) VALUES (1, ?, 0, ?, ?, ?)",
                         (uuid, db.now() + 60, number, rid))
            conn.commit()
            return number
        finally:
            conn.close()

    def test_validation_auth_and_round_trip(self):
        self.assertEqual(self.http.put('/api/client-rates/1/caller-id', json={'numbers': ''}).status_code, 401)
        self.assertEqual(self.save('+12025550101\n12025550101;12025550102').status_code, 200)
        rows = self.http.get('/api/client-rates', auth=('test', 'test-password')).json()
        self.assertEqual(rows[0]['caller_id_pool'], '12025550101\n12025550102')
        for invalid in ['abc', '0', '481234567,ignore_early_media=true', '1234567890123456', '１２３４５６７８９']:
            self.assertEqual(self.save(invalid).status_code, 422, invalid)
        self.assertEqual(self.save('1' * 4001).status_code, 422)
        self.assertEqual(self.save('\n'.join(str(12025550100 + i) for i in range(201))).status_code, 422)
        self.assertEqual(self.save('', 999).status_code, 404)
        self.assertEqual(self.choose('unchanged'), '12025550101')

    def test_round_robin_retry_restart_and_route_isolation(self):
        self.save('12025550101\n12025550102')
        self.assertEqual(self.choose('a'), '12025550101')
        self.assertEqual(self.choose('a'), '12025550101')
        db.init_db()
        self.assertEqual(self.choose('b'), '12025550102')
        self.assertEqual(self.choose('c'), '12025550101')
        self.assertEqual(self.choose('d', 2), '')
        self.save('12025550101\n12025550102')
        self.assertEqual(self.choose('e'), '12025550102')
        self.save('12025550103')
        self.assertEqual(self.choose('a'), '12025550101')
        self.assertEqual(self.choose('f'), '12025550103')
        self.save('')
        self.assertEqual(self.choose('g'), '')

    def test_parallel_calls_are_evenly_distributed(self):
        pool = ['12025550101', '12025550102', '12025550103']
        self.save('\n'.join(pool))
        with ThreadPoolExecutor(max_workers=6) as executor:
            values = list(executor.map(self.choose, [str(i) for i in range(30)]))
        self.assertEqual(Counter(values), Counter({number: 10 for number in pool}))

    def test_reserve_keeps_original_clid_rates_and_money(self):
        def reserve(uuid):
            return self.http.post('/api/reserve', headers={'X-Api-Key': 'test-api-key'}, json={
                'sip_ip': '192.0.2.1', 'destination': '48123456789', 'call_uuid': uuid, 'clid': '12025550109',
            })
        plain = reserve('plain')
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual(plain.json()['outbound_caller_id'], '')
        with db.get_conn() as conn:
            conn.execute('DELETE FROM reservations')
        self.save('12025550101\n12025550102')
        overridden = reserve('override')
        self.assertEqual(overridden.status_code, 200, overridden.text)
        self.assertEqual(overridden.json()['outbound_caller_id'], '12025550101')
        for key in plain.json().keys() - {'call_uuid', 'outbound_caller_id'}:
            self.assertEqual(plain.json()[key], overridden.json()[key], key)
        with db.get_conn() as conn:
            conn.execute('UPDATE clients SET active = 0 WHERE id = 1')
        denied = reserve('denied')
        self.assertEqual(denied.status_code, 403)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT balance_cents FROM clients').fetchone()[0], 100000)
            self.assertEqual(conn.execute("SELECT clid FROM sip_hits WHERE call_uuid = 'override'").fetchone()[0], '12025550109')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM cdr').fetchone()[0], 0)
        self.assertEqual(self.choose('next'), '12025550102')

    def test_migration_preserves_existing_rows_and_is_repeatable(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript('CREATE TABLE client_rates (id INTEGER PRIMARY KEY, sell_rate_cents INTEGER);'
                           'CREATE TABLE reservations (id INTEGER PRIMARY KEY, reserved_cents INTEGER);'
                           'INSERT INTO client_rates VALUES (1, 1430); INSERT INTO reservations VALUES (1, 12345);')
        caller_id.init_schema(conn)
        caller_id.init_schema(conn)
        self.assertEqual(dict(conn.execute('SELECT * FROM client_rates').fetchone()),
                         {'id': 1, 'sell_rate_cents': 1430, 'caller_id_pool': '', 'caller_id_cursor': 0})
        self.assertEqual(conn.execute('SELECT reserved_cents FROM reservations').fetchone()[0], 12345)

    def test_lua_bridge_for_gateway_and_direct_ip(self):
        from lupa import LuaRuntime
        source = Path('freeswitch/billing.lua').read_text()
        for gateway, ip in [('test-gateway', ''), ('', '192.0.2.20'), ('192.0.2.20:5060', '')]:
            for number in ['', '12025550101']:
                lua = LuaRuntime(unpack_returned_tuples=True)
                response = json.dumps({'max_seconds': 60, 'sell_rate_cents': 2000, 'cost_rate_cents': 1000,
                                       'client_id': 1, 'gateway_name': gateway, 'route_ip': ip,
                                       'provider_number': '48123456789', 'outbound_caller_id': number})
                lua.globals().response = response
                lua.execute('''
                    actions = {}
                    session = {
                      getVariable=function(_, k)
                        if k == 'uuid' then return 'test' end
                        if k == 'sip_local_network_addr' then return '192.0.2.10' end
                        return ''
                      end,
                      execute=function(_, k, v) actions[k]=v; if k == 'bridge' then error('TEST_BRIDGE') end end,
                      hangup=function() error('UNEXPECTED_HANGUP') end
                    }
                    freeswitch = {consoleLog=function() end}
                    io.open = function(path)
                      return {read=function() if path:find('key') then return 'test-key' end return response end, close=function() end}
                    end
                    io.popen = function() return {read=function() return '200' end, close=function() end} end
                    os.remove = function() end
                ''')
                with self.assertRaisesRegex(Exception, 'TEST_BRIDGE'):
                    lua.execute(source)
                target = lua.globals().actions['bridge']
                self.assertIn('48123456789', target)
                if number:
                    self.assertTrue(target.startswith('{origination_caller_id_number=' + number))
                    self.assertIn('sip_invite_from_uri=sip:' + number + '@192.0.2.10', target)
                else:
                    self.assertTrue(target.startswith('sofia/'))


if __name__ == '__main__':
    unittest.main()
