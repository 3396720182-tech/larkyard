"""生图网关（ticketd/gateway.py，docs/spec.md §12「生图网关」）：心跳成功后领网关令牌、存进 worker 读不到的控制台目录、
每轮给 worker 签只对这一单有效的子令牌、被吊销后重新领。假控制台见 test_console.FakeConsole。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import time

from test_console import TOKEN, ConsoleBase

from unittest import mock

from ticketd import config, console, gateway, perms, worker

REAL_ENSURE = gateway.ensure   # ConsoleBase 默认把它换成空的（别的用例不领），这里换回真的

KEY = "0123456789abcdef" * 4
GTOK = "lyg_" + "A" * 43
CLAIM = {"gateway_token": GTOK, "base_url": "https://console.example/api/v3", "sub_id": 7, "sub_key": KEY,
         "sub_ttl_max": 43200}


def console_sub_token(sub_id, sub_key, ticket, exp):
    """控制台 console/gateway/core.py Gateway.sub_token 的原样副本：两边必须逐字节一致。"""
    msg = "lys_1.%d.%s.%d" % (sub_id, ticket, exp)
    mac = base64.urlsafe_b64encode(hmac.new(sub_key.encode("ascii"), msg.encode("ascii"), hashlib.sha256).digest())
    return msg + "." + mac.rstrip(b"=").decode("ascii")


class GatewayTest(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.real = mock.patch.object(gateway, "ensure", REAL_ENSURE)
        self.real.start()
        self.enrolled_state()
        self.fake.routes[("POST", gateway.CLAIM_PATH)] = (200, dict(CLAIM))
        self.fake.routes[("GET", gateway.ME_PATH)] = (200, {"member": True})

    def tearDown(self):
        self.real.stop()   # 先于基类停掉它自己的替身，按相反顺序恢复
        super().tearDown()

    def beat(self, now=None):
        out = console.send_heartbeat(self.con, now)
        self.assertTrue(out["ok"], out)
        return out

    def events(self):
        return [(r["code"], r["detail"]) for r in self.con.execute(
            "SELECT code, detail FROM events WHERE kind='console' ORDER BY id")]

    def ticket(self):
        tid = self.new_ticket()
        return self.con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()

    def test_heartbeat_claims_once_and_stores_private(self):
        self.beat()
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat", gateway.CLAIM_PATH])
        c = self.fake.calls[1]
        self.assertEqual((c["method"], c["headers"]["Authorization"]), ("POST", "Bearer " + TOKEN))
        st = os.stat(str(gateway.path()))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(gateway.path().parent, console.console_dir())  # worker 读不到的目录
        self.assertEqual(gateway.load()["gateway_token"], GTOK)
        self.assertIn(("gateway", "已领到生图网关令牌：worker 每轮拿只对这一单有效的子令牌"), self.events())
        self.fake.calls.clear()
        self.beat()
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat"])
        self.assertIn("已领到网关令牌", "\n".join(console.status_lines(self.con)))

    def test_worker_env_has_sub_token_matching_console_format(self):
        t = self.ticket()
        self.assertNotIn(gateway.ENV_TOKEN, worker.worker_env(t))  # 还没领到
        self.beat()
        now = time.time()
        env = gateway.worker_env(t["id"], now)
        exp = int(now + config.turn_timeout() + gateway.TTL_MARGIN)
        self.assertEqual(env[gateway.ENV_TOKEN], console_sub_token(7, KEY, t["id"], exp))
        self.assertEqual(env[gateway.ENV_URL], "https://console.example/api/v3")
        wenv = worker.worker_env(t)
        self.assertTrue(wenv[gateway.ENV_TOKEN].startswith("lys_1.7.%s." % t["id"]))
        self.assertNotIn(GTOK, json.dumps(wenv))       # 网关令牌、钥匙本身不进 worker
        self.assertNotIn(KEY, json.dumps(wenv))
        self.assertNotIn(TOKEN, json.dumps(wenv))

    def test_ttl_capped(self):
        self.beat()
        os.environ["TICKETD_TURN_TIMEOUT"] = str(48 * 3600)
        try:
            tok = gateway.worker_env("T1", 1000.0)[gateway.ENV_TOKEN]
        finally:
            del os.environ["TICKETD_TURN_TIMEOUT"]
        self.assertEqual(tok.split(".")[3], str(1000 + gateway.TTL_MAX))

    def test_daemon_env_gateway_vars_not_inherited(self):
        t = self.ticket()
        os.environ[gateway.ENV_TOKEN] = "lys_stale"
        try:
            self.assertNotIn(gateway.ENV_TOKEN, worker.worker_env(t))
        finally:
            del os.environ[gateway.ENV_TOKEN]

    def test_no_env_when_stopped_or_not_enrolled_or_bad_ticket(self):
        self.beat()
        self.assertEqual(gateway.worker_env("not-a-ticket"), {})
        console._write_private(console.stopped_path(), b"DeviceRevoked\n")
        self.assertEqual(gateway.worker_env("T1"), {})
        console.remove_local()  # --leave：控制台目录连同网关令牌一起删
        self.assertEqual(gateway.worker_env("T1"), {})
        self.assertFalse(gateway.path().exists())

    def test_old_console_or_failure_retries_hourly(self):
        self.fake.routes[("POST", gateway.CLAIM_PATH)] = (404, {"error": {"code": "NotFound"}})
        t0 = time.time()
        self.beat(t0)
        self.assertIsNone(gateway.load())
        self.assertTrue(any(c == "gateway_failed" for c, _ in self.events()))
        self.fake.calls.clear()
        self.beat(t0 + 600)
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat"])   # 一小时内不再领
        self.fake.routes[("POST", gateway.CLAIM_PATH)] = (200, dict(CLAIM))
        self.beat(t0 + 3700)
        self.assertIsNotNone(gateway.load())

    def test_response_without_sub_key_not_saved(self):
        self.fake.routes[("POST", gateway.CLAIM_PATH)] = (200, {"gateway_token": GTOK,
                                                                "base_url": "https://console.example/api/v3"})
        self.beat()
        self.assertIsNone(gateway.load())
        self.assertFalse(gateway.path().exists())

    def test_bad_response_not_saved(self):
        for bad in ({"base_url": "http://x/api/v3"}, {"sub_key": "short"}, {"sub_id": True}, {"gateway_token": "x"}):
            self.con.execute("DELETE FROM kv WHERE key='console.gateway_claim_at'")
            self.fake.routes[("POST", gateway.CLAIM_PATH)] = (200, dict(CLAIM, **bad))
            self.beat()
            self.assertIsNone(gateway.load(), bad)
            self.assertFalse(gateway.path().exists(), bad)

    def test_revoked_dropped_then_reclaimed(self):
        t0 = time.time()
        self.beat(t0)
        self.fake.routes[("GET", gateway.ME_PATH)] = (401, {"error": {"code": "GatewayUnauthorized"}})
        self.fake.calls.clear()
        self.beat(t0 + 3600)
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat"])  # 不到 6 小时不查
        self.beat(t0 + gateway.CHECK_EVERY + 1)
        self.assertEqual(self.fake.paths()[-1], gateway.ME_PATH)
        self.assertEqual(self.fake.calls[-1]["headers"]["Authorization"], "Bearer " + GTOK)
        self.assertIsNone(gateway.load())
        self.beat(t0 + gateway.CHECK_EVERY + 400)
        self.assertEqual(self.fake.paths()[-1], gateway.CLAIM_PATH)
        self.assertIsNotNone(gateway.load())

    def test_check_ok_or_network_error_keeps(self):
        t0 = time.time()
        self.beat(t0)
        self.beat(t0 + gateway.CHECK_EVERY + 1)
        self.assertEqual(self.fake.paths()[-1], gateway.ME_PATH)
        self.fake.routes[("GET", gateway.ME_PATH)] = (503, {"error": {"code": "GatewayUnavailable"}})
        self.beat(t0 + 2 * gateway.CHECK_EVERY + 2)
        self.assertIsNotNone(gateway.load())

    def test_worker_cannot_read_gateway_file(self):
        s = perms.build_settings(str(self.tmp / "work" / "T1"), [str(self.tmp)])
        d = str(console.console_dir().absolute())
        self.assertIn(d, s["sandbox"]["filesystem"]["denyRead"])
        self.assertIn("Read(/%s/**)" % d, s["permissions"]["deny"])
