"""控制台接入（ticketd/console.py）：连接和证书、本机文件、入网、退出、心跳、显示、和自动更新 / 匿名统计的关系。
对应 openspec/changes/add-console-enrollment/specs/ 的 console-enrollment 和 console-heartbeat 的各个场景。
用替换 console._open 的假控制台（不起服务、不碰网络）；证书相关的用例在 _open 下面换假 socket / ssl。"""
from __future__ import annotations

import contextlib
import http.client as http_client
import io
import json
import os
import shutil
import socket
import ssl
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from helpers import REPO, TicketdTestCase
import signing

from ticketd import cli, config, console, core, db, gateway, host, metrics, telemetry, templates, update, updsig

URL = "https://console.example.com"
CODE = "LY-ABCD-EFGH-JKMN"
OWNER = "ou_" + "0123456789abcdef" * 2   # 拼出来的假 open_id（源码里不出现完整的样子）
TOKEN = "lyd_" + "T0kenT0kenT0kenT0kenT0kenT0kenT0ken12"
DEVICE = "dev_0000000001"
SECRET = "机密-XYZ123"
ORIGINAL_OPEN = console._open  # 默认实现（setUp 换成假控制台；证书用例换回它、在下面一层换假 socket / ssl）
ALL_CODES = ("CodeInvalid", "CodeStartLimit", "ConsentUnknown", "ConfirmDeliveryFailed", "ConfirmWrong",
             "EnrollExpired", "DeviceUnknown", "DeviceRevoked", "DeviceLeft", "UnknownField", "BadValue",
             "TooFrequent", "Stale", "RateLimited", "Unavailable")


class Keys:
    dir = None

    @classmethod
    def get(cls):
        if cls.dir is None or not cls.dir.exists():  # 别的测试模块（test_skills）也用：上一个模块结束时删过
            cls.dir = Path(tempfile.mkdtemp(prefix="ticketd-console-keys-"))
            for n in ("main", "backup", "evil"):
                setattr(cls, n, signing.keygen(cls.dir, n))
        return cls


def tearDownModule():
    if Keys.dir is not None:
        shutil.rmtree(str(Keys.dir), ignore_errors=True)


def err(code, status=400):
    return status, {"error": {"code": code, "message": "说明"}}


class FakeConsole:
    """替换 console._open：按 (方法, 路径) 回应，记下每个请求。处理函数返回 (状态码, JSON 对象或 bytes)，
    或抛出异常（模拟网络 / 证书失败）；给一个列表时依次取（最后一个重复用）。"""

    def __init__(self, keys):
        self.calls = []
        self.lock = threading.Lock()
        self.routes = {
            ("GET", "/api/v1/release-keys"): (200, {"main": signing.pub(keys.main), "backup": signing.pub(keys.backup)}),
            ("POST", "/api/v1/enroll/start"): (200, {"enroll_id": "enr_0123456789abcdef", "marker": "K7QD",
                                                     "person_hint": "张*", "confirm_ttl": 600}),
            ("POST", "/api/v1/enroll/confirm"): (200, {"device_id": DEVICE, "device_token": TOKEN,
                                                       "heartbeat_every": 300}),
            ("POST", "/api/v1/heartbeat"): (200, {"ok": True, "next_in": 300}),
            ("POST", "/api/v1/device/leave"): (200, {"ok": True}),
        }

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        with self.lock:
            self.calls.append({"method": method, "url": url, "path": u.path, "headers": dict(headers),
                               "body": body, "timeout": timeout})
        h = self.routes.get((method, u.path))
        if h is None:
            return 404, {}, json.dumps({"error": {"code": "NotFound", "message": ""}}).encode()
        if isinstance(h, list):
            h = h.pop(0) if len(h) > 1 else h[0]
        if callable(h):
            h = h(json.loads(body) if body else None, headers)
        if isinstance(h, BaseException):
            raise h
        status, obj = h
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return status, {"content-type": "application/json"}, raw

    def paths(self):
        return [c["path"] for c in self.calls]

    def bodies(self, path):
        return [json.loads(c["body"]) for c in self.calls if c["path"] == path]


class ConsoleBase(TicketdTestCase):
    def setUp(self):
        super().setUp()
        self.k = Keys.get()
        os.environ["TICKETD_OWNER"] = OWNER
        os.environ.pop("TICKETD_UPDATE_URL", None)
        self.fake = FakeConsole(self.k)
        self._patches = [mock.patch.object(console, "_open", self.fake),
                         mock.patch.object(console, "_stdin_is_tty", return_value=True),
                         # 心跳成功后领生图网关令牌：在 test_gateway 里单独测，其余用例不领（请求顺序不变）
                         mock.patch.object(gateway, "ensure", return_value=None)]
        for p in self._patches:
            p.start()
        self.out = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def say(self, *a, **k):
        self.out.append(" ".join(str(x) for x in a))

    def enroll(self, answers=("yes", "123456"), url=URL, code=CODE, main=None, backup=None, hostname="test-host"):
        main = main or signing.fp(self.k.main)
        backup = backup or signing.fp(self.k.backup)
        with mock.patch.object(console, "_ask", side_effect=list(answers)):
            lines = console.enroll(self.con, url, code, main, backup, hostname=hostname, out=self.say)
        self.out += lines
        return lines

    def enrolled_state(self):
        console.save({"url": URL, "device_id": DEVICE, "enrolled_at": updsig.iso(time.time()),
                      "consent_version": "c1"}, TOKEN)

    def nothing_written(self):
        self.assertFalse(console.console_dir().exists())
        self.assertFalse(update.main_signers().exists())
        self.assertFalse(update.backup_signers().exists())

    def cli(self, *argv):
        buf, ebuf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(ebuf):
            code = cli.main(list(argv))
        return code, buf.getvalue() + ebuf.getvalue()


# ---------- 连接 ----------
class FakeRaw:
    """假的 TCP socket：记下写出的字节。"""

    def __init__(self):
        self.sent = b""
        self.closed = False

    def sendall(self, b):
        self.sent += b

    send = sendall

    def close(self):
        self.closed = True


class ConnectionTest(ConsoleBase):
    def test_http_is_refused_without_any_request(self):
        with self.assertRaises(console.ConsoleError):
            console.request("GET", "/api/v1/release-keys", base="http://console.example.com")
        with self.assertRaises(ValueError) as cm:
            self.enroll(url="http://console.example.com")
        self.assertIn("https", str(cm.exception))
        self.assertEqual(self.fake.calls, [])
        self.nothing_written()

    def test_context_always_verifies(self):
        ctx = console._ssl_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        src = (REPO / "ticketd" / "console.py").read_text(encoding="utf-8")
        for bad in ("CERT_NONE", "_create_unverified_context", "check_hostname = False", "--insecure", "--pin"):
            self.assertNotIn(bad, src)

    def test_cert_failure_writes_no_request_bytes_and_does_not_retry(self):
        raw = FakeRaw()
        connects = []

        class Ctx:
            check_hostname, verify_mode = True, ssl.CERT_REQUIRED

            def wrap_socket(self, sock, server_hostname=None):
                raise ssl.SSLCertVerificationError(1, "certificate verify failed: Hostname mismatch")

        def connect(h, p, t):
            connects.append((h, p))
            return raw
        with mock.patch.object(console, "_open", ORIGINAL_OPEN), mock.patch.object(console, "_connect", connect), \
                mock.patch.object(console, "_ssl_context", lambda: Ctx()), \
                mock.patch.object(console, "ca_problem", return_value=None):
            with self.assertRaises(console.ConsoleError) as cm:
                console.request("POST", "/api/v1/heartbeat", {"schema": 1}, base=URL, token=TOKEN)
        self.assertEqual(cm.exception.code, "CertInvalid")
        self.assertIn("证书校验失败", cm.exception.text())
        self.assertEqual(raw.sent, b"")          # 一个请求字节都没写出
        self.assertTrue(raw.closed)
        self.assertEqual(connects, [("console.example.com", 443)])  # 不重试、不降级

    def test_real_tls_handshake_happens_before_request(self):
        """真的 ssl 模块：对端不是 TLS，握手失败；对端收到的只有 ClientHello，没有请求行、头和请求体。"""
        a, b = socket.socketpair()
        got = []

        def server():
            try:
                b.settimeout(5)
                got.append(b.recv(65536))
                b.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                b.settimeout(0.5)
                while True:
                    chunk = b.recv(65536)
                    if not chunk:
                        break
                    got.append(chunk)
            except OSError:
                pass
            finally:
                b.close()
        th = threading.Thread(target=server)
        th.start()
        try:
            with mock.patch.object(console, "_open", ORIGINAL_OPEN), \
                    mock.patch.object(console, "_connect", lambda h, p, t: a), \
                    mock.patch.object(console, "ca_problem", return_value=None):
                with self.assertRaises(console.ConsoleError) as cm:
                    console.request("POST", "/api/v1/heartbeat", {"secret": SECRET}, base=URL, token=TOKEN)
        finally:
            th.join(5)
        self.assertEqual(cm.exception.code, "CertInvalid")
        data = b"".join(got)
        self.assertTrue(data.startswith(b"\x16\x03"))  # TLS 握手记录
        for leak in (b"POST", b"/api/v1", TOKEN.encode(), SECRET.encode("utf-8"), b"Authorization"):
            self.assertNotIn(leak, data)

    def test_no_ca_gives_reason_and_fix(self):
        paths = mock.Mock(cafile="/nonexistent/cert.pem", capath="/nonexistent/certs")
        msg = console.ca_problem(paths=paths, ca_count=0)
        self.assertIn("找不到可用的 CA 证书", msg)
        self.assertIn("Install Certificates", msg)
        self.assertIsNone(console.ca_problem(paths=paths, ca_count=3))
        # 入网时证书校验失败、本机又没有 CA：提示原因和处理办法
        self.fake.routes[("GET", "/api/v1/release-keys")] = ssl.SSLCertVerificationError(1, "unable to get local issuer")
        with mock.patch.object(console, "ca_problem", return_value=msg):
            with self.assertRaises(console.ConsoleError) as cm:
                self.enroll()
            self.assertEqual(cm.exception.code, "NoCA")
            self.assertIn("Install Certificates", cm.exception.text())
            # ticket doctor 的控制台一项给出同样的提示（未入网时不算不通过）
            line = console.doctor_line(self.con)
            self.assertTrue(line.startswith("ℹ️ 控制台：未入网"), line)
            self.assertIn("Install Certificates", line)
            self.enrolled_state()
            line = console.doctor_line(self.con)
            self.assertTrue(line.startswith("❌"), line)
            self.assertIn("Install Certificates", line)

    def test_response_limits_and_errors(self):
        self.fake.routes[("GET", "/x")] = (200, b"{" + b" " * (70 * 1024) + b"}")
        with self.assertRaises(console.ConsoleError) as cm:
            console.request("GET", "/x", base=URL)
        self.assertEqual(cm.exception.code, "BadResponse")
        self.fake.routes[("GET", "/x")] = (200, b"<html>")
        with self.assertRaises(console.ConsoleError) as cm:
            console.request("GET", "/x", base=URL)
        self.assertEqual(cm.exception.code, "BadResponse")
        self.fake.routes[("GET", "/x")] = err("RateLimited", 429)
        with self.assertRaises(console.ConsoleError) as cm:
            console.request("GET", "/x", base=URL)
        self.assertEqual((cm.exception.code, cm.exception.status), ("RateLimited", 429))
        self.fake.routes[("GET", "/x")] = (502, b"bad gateway")
        with self.assertRaises(console.ConsoleError) as cm:
            console.request("GET", "/x", base=URL)
        self.assertEqual(cm.exception.code, "HttpError")
        self.fake.routes[("GET", "/x")] = OSError("connection refused")
        with self.assertRaises(console.ConsoleError) as cm:
            console.request("GET", "/x", base=URL)
        self.assertEqual(cm.exception.code, "Network")

    def test_every_code_has_a_chinese_hint(self):
        for c in ALL_CODES:
            self.assertIn(c, console.HINTS)
            self.assertRegex(console.HINTS[c], r"[\u4e00-\u9fff]")


# ---------- 本机文件 ----------
class LocalFilesTest(ConsoleBase):
    def test_permissions_and_atomic_files(self):
        self.enrolled_state()
        self.assertEqual(stat.S_IMODE(console.console_dir().stat().st_mode), 0o700)
        for p in (console.info_path(), console.token_path()):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in sorted(console.console_dir().iterdir())], ["console.json", "device-token"])
        self.assertTrue(console.enrolled())
        self.assertEqual(console.read_token(), TOKEN)
        console.remove_local()
        self.assertFalse(console.enrolled())
        self.assertFalse(console.console_dir().exists())

    def test_console_address_cannot_come_from_config(self):
        """场景：试图用环境变量指定控制台。"""
        config.home().mkdir(parents=True, exist_ok=True)
        config.config_file().write_text("TICKETD_CONSOLE_URL=https://example.com\n", encoding="utf-8")
        os.environ["TICKETD_CONSOLE_URL"] = "https://example.com"
        self.assertFalse(console.enrolled())
        beat = console.Heartbeat(clock=lambda: time.time() + 10 ** 6)
        for _ in range(5):
            beat.tick()
        self.assertEqual(self.fake.calls, [])
        _, out = self.cli("doctor")
        self.assertIn("TICKETD_CONSOLE_URL：没有这个配置项", out)
        _, out = self.cli("console", "status")
        self.assertIn("未入网", out)
        self.assertEqual(self.fake.calls, [])

    def test_repo_has_no_console_address(self):
        for p in list((REPO / "ticketd").glob("*.py")) + [REPO / "config.example.env"]:
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("TICKETD_CONSOLE_URL=", text, p)
        src = (REPO / "ticketd" / "console.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"https://[a-z0-9.-]+\.[a-z]{2,}(?<!example\.com)/")

    def test_token_never_in_any_output(self):
        lines = self.enroll()
        outs = list(self.out) + lines
        console.send_heartbeat(self.con)
        for argv in (("console", "show"), ("console", "status"), ("doctor",)):
            outs.append(self.cli(*argv)[1])
        outs.append(json.dumps([dict(r) for r in self.con.execute("SELECT * FROM events")], ensure_ascii=False))
        outs.append(json.dumps([dict(r) for r in self.con.execute("SELECT * FROM kv")], ensure_ascii=False))
        for o in outs:
            self.assertNotIn(TOKEN, o)
            self.assertNotIn("123456", o)  # 确认码也不进输出、日志和事件
            self.assertNotIn(CODE, o)


# ---------- 入网 ----------
class EnrollTest(ConsoleBase):
    def test_happy_path(self):
        lines = self.enroll()
        self.assertEqual(self.fake.paths(), ["/api/v1/release-keys", "/api/v1/enroll/start", "/api/v1/enroll/confirm"])
        # 清单原样打印（参与哈希的那段文本），在任何请求之前
        self.assertIn(console.CONSENT_TEXT[:-1], self.out)
        start = self.fake.bodies("/api/v1/enroll/start")[0]
        self.assertEqual(set(start), {"code", "hostname", "os", "larkyard_version", "bot_open_id", "consent"})
        self.assertEqual(start["consent"], {"version": "c2", "sha256": console.consent_sha256("c2")})
        self.assertRegex(start["consent"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual((start["code"], start["hostname"], start["bot_open_id"]), (CODE, "test-host", OWNER))
        self.assertEqual(set(start["os"]), {"name", "version", "arch"})
        for v in start["os"].values():
            self.assertRegex(v, console.OS_FIELD_RE)
        self.assertRegex(start["larkyard_version"], console.VERSION_RE)
        self.assertEqual(self.fake.bodies("/api/v1/enroll/confirm")[0],
                         {"enroll_id": "enr_0123456789abcdef", "confirm_code": "123456"})
        self.assertTrue(any("确认码已私信给 张*，会话标记 K7QD" in o for o in self.out), self.out)
        self.assertTrue(any("不要告诉任何人" in o for o in self.out))
        info = json.loads(console.info_path().read_text(encoding="utf-8"))
        self.assertEqual(set(info), {"url", "device_id", "enrolled_at", "consent_version"})
        self.assertEqual((info["url"], info["device_id"], info["consent_version"]), (URL, DEVICE, "c2"))
        # 入网之前的出错记录不上报：游标从入网时算起
        self.assertIsNotNone(console._cursor(self.con))
        self.assertEqual(console.read_token(), TOKEN)
        self.assertEqual(updsig.signer_key(update.main_signers()), updsig.parse_pubkey(signing.pub(self.k.main)))
        self.assertEqual(updsig.signer_key(update.backup_signers()), updsig.parse_pubkey(signing.pub(self.k.backup)))
        ev = self.con.execute("SELECT code FROM events WHERE kind='console'").fetchall()
        self.assertEqual([r["code"] for r in ev], ["enrolled"])
        # 场景：入网但没转受管安装 —— 最后一行提示怎么启用自动更新
        self.assertIn("不是受管安装", lines[-1])
        self.assertIn("ticket update adopt", lines[-1])
        self.assertTrue(any(l.startswith("ℹ️ 自动更新：未启用") for l in [update.doctor_line(self.con)]))
        self.assertIn("未启用", update.status_lines(self.con)[0])

    def test_start_request_is_strict(self):
        self.enroll(hostname="My Mac (2)!.local")
        start = self.fake.bodies("/api/v1/enroll/start")[0]
        self.assertEqual(start["hostname"], "MyMac2.local")

    def test_hostname_empty_after_cleanup(self):
        with self.assertRaises(ValueError) as cm:
            self.enroll(hostname="我的电脑")
        self.assertIn("--hostname", str(cm.exception))
        self.assertEqual(self.fake.calls, [])

    def test_bad_owner_is_local_error(self):
        os.environ["TICKETD_OWNER"] = "ou_owner"
        with self.assertRaises(ValueError) as cm:
            self.enroll()
        self.assertIn("TICKETD_OWNER", str(cm.exception))
        self.assertEqual(self.fake.calls, [])

    def test_decline(self):
        """场景：不同意。"""
        for answer in ("no", "", "y", "YES "):
            self.out = []
            lines = self.enroll(answers=(answer,))
            self.assertIn("没有同意", lines[-1])
            self.assertEqual(self.fake.calls, [])
            self.nothing_written()

    def test_pipe_is_refused(self):
        """场景：在管道里执行。"""
        with mock.patch.object(console, "_stdin_is_tty", return_value=False):
            code, out = self.cli("enroll", URL, CODE, "--release-key", signing.fp(self.k.main),
                                 "--backup-key", signing.fp(self.k.backup))
        self.assertEqual(code, 2)
        self.assertIn("终端里由本人确认", out)
        self.assertEqual(self.fake.calls, [])
        self.nothing_written()

    def test_fingerprint_mismatch(self):
        """场景：指纹对不上。"""
        with self.assertRaises(ValueError) as cm:
            self.enroll(main=signing.fp(self.k.evil))
        self.assertIn("发版公钥指纹不一致", str(cm.exception))
        self.assertEqual(self.fake.paths(), ["/api/v1/release-keys"])  # 入网码没有提交
        self.nothing_written()

    def test_bad_fingerprint_format(self):
        with self.assertRaises(ValueError):
            self.enroll(main="abc")
        self.assertEqual(self.fake.calls, [])

    def test_already_trusted_same_pair(self):
        """场景：本机已信任同一对公钥。"""
        update.trust(self.con, signing.pub(self.k.main), signing.fp(self.k.main),
                     signing.pub(self.k.backup), signing.fp(self.k.backup))
        before = (update.main_signers().read_bytes(), update.main_signers().stat().st_mtime_ns,
                  update.backup_signers().read_bytes())
        self.enroll()
        self.assertTrue(console.enrolled())
        self.assertEqual(before, (update.main_signers().read_bytes(), update.main_signers().stat().st_mtime_ns,
                                  update.backup_signers().read_bytes()))

    def test_already_trusted_other_pair(self):
        update.trust(self.con, signing.pub(self.k.evil), signing.fp(self.k.evil),
                     signing.pub(self.k.backup), signing.fp(self.k.backup))
        before = update.main_signers().read_bytes()
        with self.assertRaises(ValueError) as cm:
            self.enroll()
        self.assertIn("另一对发版公钥", str(cm.exception))
        self.assertNotIn("/api/v1/enroll/start", self.fake.paths())
        self.assertEqual(update.main_signers().read_bytes(), before)
        self.assertFalse(console.console_dir().exists())

    def test_someone_elses_code(self):
        """场景：用别人的入网码 —— 终端显示 A 的打码姓名，B 拿不到确认码，入网失败。"""
        self.fake.routes[("POST", "/api/v1/enroll/start")] = (200, {"enroll_id": "enr_0123456789abcdef",
                                                                     "marker": "Q2WX", "person_hint": "王*",
                                                                     "confirm_ttl": 600})
        self.fake.routes[("POST", "/api/v1/enroll/confirm")] = [err("ConfirmWrong")] * 4 + [err("EnrollExpired")]
        with self.assertRaises(ValueError) as cm:
            self.enroll(answers=["yes"] + ["000000"] * 5)
        self.assertIn("请重新执行 ticket enroll", str(cm.exception))
        self.assertTrue(any("确认码已私信给 王*" in o for o in self.out))
        self.assertEqual(self.fake.paths().count("/api/v1/enroll/confirm"), 5)
        self.nothing_written()

    def test_wrong_then_right(self):
        self.fake.routes[("POST", "/api/v1/enroll/confirm")] = [
            err("ConfirmWrong"), (200, {"device_id": DEVICE, "device_token": TOKEN, "heartbeat_every": 300})]
        self.enroll(answers=["yes", "12345", "111111", "123456"])  # 不是 6 位的不发请求
        self.assertEqual(self.fake.paths().count("/api/v1/enroll/confirm"), 2)
        self.assertTrue(any(console.HINTS["ConfirmWrong"] in o for o in self.out))
        self.assertTrue(console.enrolled())

    def test_code_voided_during_confirm(self):
        self.fake.routes[("POST", "/api/v1/enroll/confirm")] = err("CodeInvalid")
        with self.assertRaises(ValueError) as cm:
            self.enroll()
        self.assertIn("入网码无效", str(cm.exception))
        self.nothing_written()

    def test_delivery_failed(self):
        """场景：确认码发不出去。"""
        self.fake.routes[("POST", "/api/v1/enroll/start")] = err("ConfirmDeliveryFailed", 502)
        code, out = self.cli_enroll()
        self.assertEqual(code, 2)
        self.assertIn("确认码发不出去，请联系管理员", out)
        self.nothing_written()

    def cli_enroll(self, answers=("yes", "123456")):
        with mock.patch.object(console, "_ask", side_effect=list(answers)):
            return self.cli("enroll", URL, CODE, "--release-key", signing.fp(self.k.main),
                            "--backup-key", signing.fp(self.k.backup), "--hostname", "test-host")

    def test_start_errors(self):
        for c in ("CodeInvalid", "CodeStartLimit", "ConsentUnknown", "UnknownField", "BadValue", "RateLimited",
                  "Unavailable"):
            self.fake.calls.clear()
            self.fake.routes[("POST", "/api/v1/enroll/start")] = err(c)
            code, out = self.cli_enroll()
            self.assertEqual(code, 2)
            self.assertIn(console.HINTS[c], out)
            self.nothing_written()

    def test_cert_failure_on_enroll(self):
        """场景：证书和地址对不上。"""
        self.fake.routes[("GET", "/api/v1/release-keys")] = ssl.SSLCertVerificationError(1, "Hostname mismatch")
        with mock.patch.object(console, "ca_problem", return_value=None):
            code, out = self.cli_enroll()
        self.assertEqual(code, 2)
        self.assertIn("证书校验失败", out)
        self.assertEqual(self.fake.paths(), ["/api/v1/release-keys"])
        self.nothing_written()

    def test_reenroll_refused(self):
        """场景：重复入网。"""
        self.enrolled_state()
        code, out = self.cli_enroll()
        self.assertEqual(code, 2)
        self.assertIn("ticket enroll --leave", out)
        self.assertEqual(self.fake.calls, [])

    def test_worker_refused(self):
        """场景：worker 试图退出（入网也一样）。"""
        self.enrolled_state()
        os.environ["TICKETD_TICKET"] = "T0001"
        code, out = self.cli("enroll", "--leave")
        self.assertEqual(code, 2)
        self.assertIn("只能由用户本人在终端执行", out)
        self.assertTrue(console.enrolled())
        code, out = self.cli("console", "status")  # 只读的可以
        self.assertEqual(code, 0)
        os.environ.pop("TICKETD_TICKET")
        console.remove_local()
        os.environ["TICKETD_SPOOL"] = str(self.tmp / "spool")
        with self.assertRaises(PermissionError):
            self.enroll()
        self.assertEqual(self.fake.calls, [])

    def test_local_write_failure_tells_device_id(self):
        with mock.patch.object(console, "save", side_effect=OSError("disk full")):
            with self.assertRaises(ValueError) as cm:
                self.enroll()
        self.assertIn(DEVICE, str(cm.exception))
        self.assertIn("吊销这台设备后重新入网", str(cm.exception))
        self.assertFalse(console.console_dir().exists())

    def test_ctrl_c_writes_nothing(self):
        with mock.patch.object(console, "_ask", side_effect=["yes", KeyboardInterrupt()]):
            code, out = self.cli("enroll", URL, CODE, "--release-key", signing.fp(self.k.main),
                                 "--backup-key", signing.fp(self.k.backup), "--hostname", "h")
        self.assertEqual(code, 2)
        self.assertIn("已取消", out)
        self.nothing_written()


# ---------- 退出 ----------
class LeaveTest(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.enroll()
        self.fake.calls.clear()

    def test_normal_leave(self):
        """场景：正常退出。"""
        code, out = self.cli("enroll", "--leave")
        self.assertEqual(code, 0, out)
        call = self.fake.calls[0]
        self.assertEqual((call["method"], call["path"], call["body"]), ("POST", "/api/v1/device/leave", b"{}"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertFalse(console.console_dir().exists())
        self.assertIn("发版公钥保留", out)
        self.assertTrue(update.main_signers().is_file())
        self.fake.calls.clear()
        beat = console.Heartbeat(clock=lambda: time.time() + 10 ** 6)
        for _ in range(5):
            beat.tick()
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(console.badge(self.con))

    def test_unreachable_asks(self):
        self.fake.routes[("POST", "/api/v1/device/leave")] = OSError("unreachable")
        with mock.patch.object(console, "_ask", return_value="no"):
            lines = console.leave(self.con, out=self.say)
        self.assertIn("没有删除本机文件", lines[0])
        self.assertTrue(console.enrolled())
        n = len(self.fake.calls)
        with mock.patch.object(console, "_stdin_is_tty", return_value=False):
            with self.assertRaises(PermissionError):  # --leave 也只能在终端里由本人执行，不发请求
                console.leave(self.con, out=self.say)
        self.assertTrue(console.enrolled())
        self.assertEqual(len(self.fake.calls), n)
        with mock.patch.object(console, "_ask", return_value="yes"):
            lines = console.leave(self.con, out=self.say)
        self.assertFalse(console.console_dir().exists())
        self.assertIn("没能通知控制台", lines[0])

    def test_already_revoked_just_cleans_up(self):
        self.fake.routes[("POST", "/api/v1/device/leave")] = err("DeviceRevoked", 401)
        with mock.patch.object(console, "_ask", side_effect=AssertionError("不该问")):
            console.leave(self.con, out=self.say)
        self.assertFalse(console.console_dir().exists())


# ---------- 心跳 ----------
class HeartbeatTest(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.enrolled_state()

    def test_whitelist(self):
        hb = console.build_heartbeat(self.con)
        self.assertEqual(set(hb), {"schema", "sent_at", "larkyard_version", "os", "doctor", "tickets", "update"})
        self.assertEqual(hb["schema"], 1)
        self.assertRegex(hb["sent_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(hb["larkyard_version"], console.VERSION_RE)
        self.assertEqual(set(hb["os"]), {"name", "version", "arch"})
        self.assertEqual(set(hb["doctor"]), {"ok", "failed"})
        self.assertLessEqual(len(hb["doctor"]["failed"]), 20)
        for f in hb["doctor"]["failed"]:
            self.assertRegex(f, console.DOCTOR_ID_RE)
            self.assertIn(f, cli.DOCTOR_IDS)
        self.assertEqual(set(hb["tickets"]), {"running", "queued"})
        self.assertEqual(set(hb["update"]), {"enabled", "channel", "last_code", "last_at", "hold_until"})
        self.assertIn(hb["update"]["channel"], ("canary", "early", "stable"))
        self.assertIsNone(hb["update"]["last_code"])
        update.event(self.con, "installed", "x", dedupe=False)
        metrics.kv_set(self.con, "update.hold_until", str(time.time() + 3600))
        hb = console.build_heartbeat(self.con)
        self.assertEqual(hb["update"]["last_code"], "installed")
        self.assertRegex(hb["update"]["last_at"], r"Z$")
        self.assertRegex(hb["update"]["hold_until"], r"Z$")
        self.assertLessEqual(len(json.dumps(hb).encode()), 8192)

    def test_secret_ticket_not_in_heartbeat(self):
        """场景：带特殊字符串的工单。"""
        tid = self.new_ticket(request="请处理 %s 的事" % SECRET, title=SECRET)
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (tid,))
        console.send_heartbeat(self.con)
        body = self.fake.calls[-1]["body"]
        self.assertNotIn(SECRET.encode("utf-8"), body)
        self.assertNotIn(SECRET, body.decode("utf-8"))
        hb = json.loads(body)
        self.assertEqual(hb["tickets"], {"running": 1, "queued": 0})
        for leak in (OWNER, DEVICE, TOKEN, tid, str(self.tmp)):
            self.assertNotIn(leak, body.decode("utf-8"))

    def test_request_shape(self):
        res = console.send_heartbeat(self.con)
        self.assertEqual(res, {"sent": True, "ok": True, "code": None, "next_in": 300, "stopped": False})
        c = self.fake.calls[-1]
        self.assertEqual((c["method"], c["url"]), ("POST", URL + "/api/v1/heartbeat"))
        self.assertEqual(c["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(c["headers"]["Content-Type"], "application/json")
        self.assertEqual(c["timeout"], 15.0)
        self.assertTrue(console.connected(self.con))
        self.assertEqual(console.state(self.con), "ok")
        self.assertEqual([r["code"] for r in self.con.execute("SELECT code FROM events WHERE kind='console'")],
                         ["started"])

    def test_show_matches_what_is_sent(self):
        """场景：对照。"""
        code, out = self.cli("console", "show")
        self.assertEqual(code, 0)
        self.assertIn(URL + "/api/v1/heartbeat", out)
        shown = json.loads(out[out.index("{"):])
        console.send_heartbeat(self.con)
        sent = json.loads(self.fake.calls[-1]["body"])
        shown.pop("sent_at"), sent.pop("sent_at")
        self.assertEqual(shown, sent)
        self.assertNotIn(TOKEN, out)

    def test_next_in(self):
        """场景：控制台要求放慢 / 给了离谱的间隔。"""
        for given, want in ((500, 500), (5, None), (300, 300), (570, 570), (571, None), ("500", None), (True, None)):
            self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": given})
            self.assertEqual(console.send_heartbeat(self.con)["next_in"], want, given)

    def _beat(self, clock, rand=lambda a, b: 10.0):
        return console.Heartbeat(clock=clock, rand=rand)

    def _drive(self, beat):
        started = beat.tick()
        if started:
            beat.thread.join(10)
        return started

    def test_schedule_follows_next_in(self):
        t = {"now": 1000.0}
        beat = self._beat(lambda: t["now"])
        self.assertFalse(beat.tick())          # 第一次：0～30 秒后
        self.assertEqual(beat.next_at, 1010.0)
        t["now"] = 1010.0
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 500})
        self.assertTrue(self._drive(beat))
        self.assertEqual(beat.next_at, 1010.0 + 500 + 10)
        t["now"] = 1519.0
        self.assertFalse(self._drive(beat))
        t["now"] = 1520.0
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 5})
        self.assertTrue(self._drive(beat))
        self.assertEqual(beat.next_at, 1520.0 + 300 + 10)  # 离谱的间隔不认，仍按 300 秒

    def test_max_gap_is_600(self):
        t = {"now": 0.0}
        beat = self._beat(lambda: t["now"], rand=lambda a, b: b)  # 抖动取最大
        beat.tick()
        t["now"] = beat.next_at
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 570})
        self._drive(beat)
        self.assertLessEqual(beat.next_at - t["now"], 600)

    def test_failure_does_not_raise_and_records_latest_error(self):
        self.fake.routes[("POST", "/api/v1/heartbeat")] = OSError("timed out")
        res = console.send_heartbeat(self.con)
        self.assertEqual((res["ok"], res["code"], res["stopped"]), (False, "Network", False))
        self.assertIn("连不上控制台", metrics.kv_get(self.con, "console.last_error"))
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (503, b"")
        console.send_heartbeat(self.con)
        self.fake.routes[("POST", "/api/v1/heartbeat")] = ssl.SSLCertVerificationError(1, "expired")
        with mock.patch.object(console, "ca_problem", return_value=None):
            console.send_heartbeat(self.con)
        self.assertIn("证书校验失败", metrics.kv_get(self.con, "console.last_error"))
        self.assertEqual(console.state(self.con), "failing")

    def test_offline_24h_sends_only_latest_once(self):
        """场景：控制台停了 24 小时 —— 不排队，恢复后只发一次当时的状态，之后恢复 5 分钟一次。"""
        t = {"now": 1_000_000.0}
        beat = self._beat(lambda: t["now"], rand=lambda a, b: 0.0)
        self.fake.routes[("POST", "/api/v1/heartbeat")] = OSError("down")
        end = t["now"] + 86400
        while t["now"] < end:
            self._drive(beat)
            t["now"] += 10
        down = len(self.fake.calls)
        self.assertLessEqual(down, 86400 // 300 + 2)   # 只是按周期尝试，没有越积越多
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE kind='console' AND code='failing'")
                         .fetchone()[0], 1)
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='queued' WHERE id=?", (tid,))
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300})
        stop = t["now"] + 299
        while t["now"] < stop:
            self._drive(beat)
            t["now"] += 10
        after = self.fake.calls[down:]
        self.assertEqual(len(after), 1)                 # 恢复后的这 5 分钟里只发了一次
        self.assertEqual(json.loads(after[0]["body"])["tickets"]["queued"], 1)  # 当时的最新状态
        self.assertEqual(console.state(self.con), "ok")
        self.assertIn("recovered", [r["code"] for r in self.con.execute("SELECT code FROM events WHERE kind='console'")])

    def test_ignores_extra_response_fields(self):
        """场景：响应里夹带指令。"""
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300, "run": "rm -rf ~",
                                                                 "config": {"TICKETD_OWNER": "x"}})
        before = sorted(str(p) for p in config.home().rglob("*"))
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen, \
                mock.patch("os.system") as system:
            res = console.send_heartbeat(self.con)
        self.assertEqual(res["ok"], True)
        run.assert_not_called(), popen.assert_not_called(), system.assert_not_called()
        self.assertEqual(sorted(str(p) for p in config.home().rglob("*")), before)
        self.assertEqual(config.owner(), OWNER)

    def test_revoked_stops(self):
        """场景：被吊销。"""
        tid = self.new_ticket()
        self.fake.routes[("POST", "/api/v1/heartbeat")] = err("DeviceRevoked", 401)
        res = console.send_heartbeat(self.con)
        self.assertTrue(res["stopped"])
        self.assertEqual(console.state(self.con), "stopped:DeviceRevoked")
        n = len(self.fake.calls)
        t = {"now": time.time() + 10 ** 5}
        beat = self._beat(lambda: t["now"])
        for _ in range(10):
            self._drive(beat)
            t["now"] += 600
        self.assertEqual(len(self.fake.calls), n)       # 不再上报
        line = console.doctor_line(self.con)
        self.assertTrue(line.startswith("❌"), line)
        self.assertIn("设备已被控制台吊销", line)
        self.assertIn("ticket enroll --leave", line)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE kind='console' AND code='stopped'")
                         .fetchone()[0], 1)
        self.assertEqual(core.get(self.con, tid)["state"], "queued")  # 派单不受影响
        self.assertIn("已停止上报", "\n".join(console.status_lines(self.con)))

    def test_stop_only_on_401(self):
        for status, c, stops in ((401, "DeviceLeft", True), (401, "DeviceUnknown", True),
                                 (403, "DeviceRevoked", False), (401, "RateLimited", False)):
            self.con.execute("DELETE FROM kv WHERE key LIKE 'console.%'")
            if console.stopped_path().exists():
                console.stopped_path().unlink()
            self.fake.routes[("POST", "/api/v1/heartbeat")] = err(c, status)
            self.assertEqual(console.send_heartbeat(self.con)["stopped"], stops, (status, c))

    def test_status(self):
        out = "\n".join(console.status_lines(self.con))
        self.assertIn("还没有成功上报", out)
        self.assertIn(DEVICE, out)
        self.assertIn(URL, out)
        console.send_heartbeat(self.con)
        out = "\n".join(console.status_lines(self.con))
        self.assertIn("正常上报", out)
        self.assertNotIn(TOKEN, out)
        self.fake.routes[("POST", "/api/v1/heartbeat")] = err("Unavailable", 503)
        console.send_heartbeat(self.con)
        out = "\n".join(console.status_lines(self.con))
        self.assertIn("最近一次错误", out)
        self.assertIn("控制台暂时不可用", out)


class NotEnrolledTest(ConsoleBase):
    def test_24h_without_enroll_sends_nothing(self):
        """场景：开源默认安装。"""
        refused = mock.Mock(side_effect=AssertionError("不该有任何到控制台的请求"))
        t = {"now": time.time()}
        with mock.patch.object(console, "_open", refused), mock.patch.object(update, "fetch") as fetch:
            beat = console.Heartbeat(clock=lambda: t["now"])
            up = update.Updater(self.con, clock=lambda: t["now"])
            for _ in range(24 * 60):
                beat.tick()
                up.tick()
                t["now"] += 60
            fetch.assert_not_called()
        refused.assert_not_called()
        _, out = self.cli("console", "status")
        self.assertIn("未入网", out)
        self.assertEqual(update.source(), ("", False))

    def test_doctor_not_enrolled(self):
        """场景：未入网。"""
        line = console.doctor_line(self.con)
        self.assertTrue(line.startswith("ℹ️ 控制台：未入网"), line)
        checks = cli.doctor_checks(self.con)
        self.assertIn(("console", line), checks)
        self.assertIsNone(console.badge(self.con))
        tid = self.new_ticket()
        from ticketd import card
        rendered = json.dumps(card.render(core.get(self.con, tid), card.view(self.con, core.get(self.con, tid),
                                                                              time.time()), [], time.time()),
                              ensure_ascii=False)
        self.assertNotIn("内部版", rendered)
        self.con.execute("UPDATE tickets SET state='running', started_at=? WHERE id=?", (time.time(), tid))
        core.add_report(self.con, tid, "ready", "完成了。细节", [], None)
        row = self.con.execute("SELECT card_json FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertNotIn("内部版", row["card_json"])

    def test_show_before_enroll_is_preview_only(self):
        code, out = self.cli("console", "show")
        self.assertEqual(code, 0)
        self.assertIn("未入网", out)
        self.assertIn('"schema": 1', out)
        self.assertEqual(self.fake.calls, [])


class DoctorTest(ConsoleBase):
    def test_worker_does_not_touch_console_files(self):
        # worker 的沙箱禁读控制台目录（stat 也会被拒）：worker 里的 doctor / console 不碰这些文件
        self.enrolled_state()
        os.environ["TICKETD_TICKET"] = "T0001"
        try:
            with mock.patch.object(console, "present", side_effect=PermissionError("denied")), \
                    mock.patch.object(console, "load", side_effect=PermissionError("denied")):
                self.assertTrue(console.doctor_line(self.con).startswith("ℹ️ 控制台：worker 的沙箱禁读"))
                for action in ("status", "show"):
                    self.assertIn("沙箱禁读控制台目录", console.command_console(self.con, action))
        finally:
            os.environ.pop("TICKETD_TICKET")

    def test_ids_are_stable_and_valid(self):
        for i in cli.DOCTOR_IDS:
            self.assertRegex(i, console.DOCTOR_ID_RE)
        for cid, line in cli.doctor_checks(self.con):
            self.assertIn(cid, cli.DOCTOR_IDS)
        hb = console.build_heartbeat(self.con)
        bad = sorted({cid for cid, line in cli.doctor_checks(self.con) if line.startswith("❌")})
        self.assertEqual(sorted(hb["doctor"]["failed"]), bad)
        self.assertEqual(hb["doctor"]["ok"], not bad)
        self.assertIn("owner", bad)  # 测试环境里 config.env 没有 TICKETD_OWNER

    def test_console_line_states(self):
        self.enrolled_state()
        self.assertTrue(console.doctor_line(self.con).startswith("⚠️ 控制台：刚入网"))
        console.send_heartbeat(self.con)
        self.assertTrue(console.doctor_line(self.con).startswith("✅ 控制台：已连接"))
        metrics.kv_set(self.con, "console.last_ok_at", str(time.time() - 3600))
        metrics.kv_set(self.con, "console.last_error", "2026-11-20T08:00:00Z 连不上控制台（Network）")
        line = console.doctor_line(self.con)
        self.assertTrue(line.startswith("❌ 控制台：最近 30 分钟没有成功上报"), line)
        self.assertIn("连不上控制台", line)
        os.environ["TICKETD_UPDATE_URL"] = "https://updates.example.com/u"
        self.assertIn("更新源不是控制台", console.doctor_line(self.con))
        self.assertNotIn(TOKEN, self.cli("doctor")[1])

    def test_broken_local_files(self):
        self.enrolled_state()
        console.token_path().unlink()
        self.assertFalse(console.enrolled())
        self.assertIn("不完整", console.doctor_line(self.con))
        lines = console.leave(self.con, out=self.say)
        self.assertFalse(console.console_dir().exists(), lines)


class BadgeTest(ConsoleBase):
    def test_badge_on_cards(self):
        self.enrolled_state()
        from ticketd import __version__, card
        self.assertEqual(console.badge(self.con), "内部版 v%s · 控制台未连接" % __version__)
        console.send_heartbeat(self.con)
        b = "内部版 v%s · 已连接控制台" % __version__
        self.assertEqual(console.badge(self.con), b)
        tid = self.new_ticket()
        t = core.get(self.con, tid)
        rendered = card.render(t, card.view(self.con, t, time.time()), [], time.time())
        self.assertEqual(rendered["body"]["elements"][-1]["text"]["content"], b)  # 状态卡：页脚最后一行
        self.con.execute("UPDATE tickets SET state='running', started_at=? WHERE id=?", (time.time(), tid))
        core.add_report(self.con, tid, "ready", "完成了。还有细节", [], None)
        c = json.loads(self.con.execute("SELECT card_json FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0])
        panels = [e for e in c["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        self.assertEqual(len(panels), 1)
        self.assertFalse(panels[0]["expanded"])
        self.assertEqual(panels[0]["elements"][-1]["text"]["content"], b)  # 结果卡：收起的详情里

    def test_with_badge_creates_panel_when_missing(self):
        c = templates.result_card("T0001", "t", None, "ready", "一句话", [], None, [], None)
        n = len(c["body"]["elements"])
        templates.with_badge(c, "内部版 v1.0.0 · 控制台未连接")
        els = c["body"]["elements"]
        self.assertEqual(len(els), n + 1)
        self.assertEqual(els[-2]["tag"], "collapsible_panel")
        self.assertEqual(els[-2]["header"]["title"]["content"], "详情")
        self.assertIn("引用此卡可追问", els[-1]["text"]["content"])  # 页脚仍在最后
        self.assertIs(templates.with_badge(c, None), c)


class UpdateSourceTest(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.enroll()
        self.fake.calls.clear()

    def test_console_is_update_source(self):
        base = URL + "/api/v1/updates"
        self.assertEqual(update.source(), (base, True))
        self.assertIsNone(update.fetch(base + "/pause.json"))   # 控制台还没有这个路径：404 当作没有
        c = self.fake.calls[-1]
        self.assertEqual((c["method"], c["url"]), ("GET", base + "/pause.json"))
        self.assertEqual(c["headers"]["Authorization"], "Bearer " + TOKEN)
        self.fake.routes[("GET", "/api/v1/updates/channels/stable/manifest.json")] = (200, b"x" * 10)
        self.assertEqual(update.fetch(base + "/channels/stable/manifest.json"), b"x" * 10)
        self.fake.routes[("GET", "/api/v1/updates/channels/stable/manifest.json")] = (200, b"x" * (70 * 1024))
        with self.assertRaises(update.FetchError):
            update.fetch(base + "/channels/stable/manifest.json")
        self.fake.routes[("GET", "/api/v1/updates/x.json")] = ssl.SSLCertVerificationError(1, "bad")
        with mock.patch.object(console, "ca_problem", return_value=None):
            with self.assertRaises(update.FetchError) as cm:
                update.fetch(base + "/x.json")
        self.assertIn("证书校验失败", str(cm.exception))
        self.fake.routes[("GET", "/api/v1/updates/channels/stable/manifest.json")] = err("DeviceRevoked", 401)
        with self.assertRaises(update.FetchError):
            update.fetch(base + "/channels/stable/manifest.json")
        self.assertIsNone(console.updates_base())  # 被吊销：之后不再经控制台更新

    def test_download_via_console(self):
        import hashlib
        data = signing.package("9.9.9")
        self.fake.routes[("GET", "/api/v1/updates/packages/larkyard-9.9.9.tar.gz")] = (200, data)
        dest = self.tmp / "pkg" / "larkyard-9.9.9.tar.gz"
        update.download(URL + "/api/v1/updates/packages/larkyard-9.9.9.tar.gz", dest, len(data),
                        hashlib.sha256(data).hexdigest())
        self.assertEqual(dest.read_bytes(), data)
        with self.assertRaises(ValueError):
            update.download(URL + "/api/v1/updates/packages/larkyard-9.9.9.tar.gz", dest, len(data) - 1, "0" * 64)

    def test_explicit_update_url_wins(self):
        os.environ["TICKETD_UPDATE_URL"] = "https://updates.example.com/u"
        self.assertEqual(update.source(), ("https://updates.example.com/u", False))
        with mock.patch.object(update, "_open", side_effect=update.urllib.error.URLError("offline")):
            with self.assertRaises(update.FetchError):
                update.fetch("https://updates.example.com/u/pause.json")
        self.assertEqual(self.fake.calls, [])  # 没经控制台

    def test_managed_enrolled_is_enabled(self):
        with mock.patch.object(update, "is_managed", return_value=True):
            self.assertEqual(update.disabled_reasons(), [])
            self.assertTrue(update.doctor_line(self.con).startswith("✅"))
        self.assertIn("不是受管安装", update.doctor_line(self.con))
        self.assertTrue(update.doctor_line(self.con).startswith("ℹ️"))


class TelemetrySeparationTest(ConsoleBase):
    def test_channels_are_independent(self):
        """场景：入网的机器也开了匿名统计。"""
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        before = telemetry.build(self.con, day, "00000000-0000-4000-8000-000000000000")
        self.enroll()
        console.send_heartbeat(self.con)
        iid = telemetry.turn_on(self.con)
        rep = telemetry.build(self.con, day, iid)
        before["install_id"] = iid
        self.assertEqual(rep, before)          # 入网不改变匿名统计的内容
        telemetry.validate(rep)
        text = json.dumps(rep, ensure_ascii=False)
        for leak in (DEVICE, OWNER, TOKEN, URL):
            self.assertNotIn(leak, text)
        hb = self.fake.calls[-1]["body"].decode("utf-8")
        self.assertNotIn(iid, hb)
        self.assertTrue(all(c["url"].startswith(URL) for c in self.fake.calls))
        self.assertNotEqual(telemetry.url(), URL)


class HostOsInfoTest(unittest.TestCase):
    def test_both_platforms(self):
        with mock.patch.object(host, "system", return_value="darwin"), \
                mock.patch("platform.mac_ver", return_value=("15.2", ("", "", ""), "arm64")), \
                mock.patch("platform.machine", return_value="arm64"):
            self.assertEqual(host.os_info(), {"name": "macOS", "version": "15.2", "arch": "arm64"})
        osr = "NAME=\"Ubuntu\"\nVERSION_ID=\"24.04\"\n"
        with mock.patch.object(host, "system", return_value="linux"), \
                mock.patch.object(host.Path, "read_text", return_value=osr), \
                mock.patch("platform.machine", return_value="x86_64"):
            self.assertEqual(host.os_info(), {"name": "Ubuntu", "version": "24.04", "arch": "x86_64"})
        with mock.patch.object(host, "system", return_value="linux"), \
                mock.patch.object(host.Path, "read_text", side_effect=OSError), \
                mock.patch("platform.release", return_value="6.8.0-1-generic"), \
                mock.patch("platform.machine", return_value="aarch64"):
            self.assertEqual(host.os_info(), {"name": "Linux", "version": "6.8.0-1-generic", "arch": "aarch64"})

    def test_fields_cleaned(self):
        with mock.patch.object(host, "os_info", return_value={"name": "Weird/OS™", "version": "", "arch": "x" * 40}):
            f = console.os_fields()
        self.assertEqual(f, {"name": "WeirdOS", "version": "unknown", "arch": "x" * 32})


class ConsentTest(unittest.TestCase):
    def test_text_matches_design(self):
        import re
        d = (REPO / "openspec" / "changes" / "add-console-enrollment" / "design.md").read_text(encoding="utf-8")
        block = re.search(r"### D4.*?```\n(.*?)```", d, re.S).group(1)
        self.assertEqual(block, console.CONSENT_C1)
        self.assertEqual(len(console.CONSENT_C1.splitlines()), 5)
        self.assertTrue(console.CONSENT_C1.endswith("。\n"))
        # c1 一个字都不能变：已入网设备的同意记录指向它
        self.assertEqual(console.consent_sha256("c1"),
                         "0ad831b7df4c8dfa5ad888fd05430495f4c0816b9936ef32a0037a046925d92a")

    def test_c2_matches_design(self):
        import re
        d = (REPO / "openspec" / "changes" / "add-console-problem-reports" / "design.md").read_text(encoding="utf-8")
        block = re.search(r"### D1.*?```\n(.*?)```", d, re.S).group(1)
        self.assertEqual(block, console.CONSENT_C2)
        self.assertEqual(console.CONSENT_VERSION, "c2")
        self.assertEqual(console.CONSENT_TEXT, console.CONSENT_C2)
        self.assertEqual(len(console.CONSENT_C2.splitlines()), 6)
        self.assertTrue(console.CONSENT_C2.endswith("。\n"))
        # 和控制台仓库登记的 c2 逐字一致（两边对照这个哈希）
        self.assertEqual(console.consent_sha256("c2"),
                         "4f85ce4332334dc76eb8c4870e2fe646866f4fe6b7f4c6b979fa3f0027760ccd")


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTest(ConsoleBase):
    """对抗审查（T0046）后补的用例。"""

    def test_whole_request_deadline(self):
        """控制台每隔一会儿挤一个字节：整个请求到点就断开，不会一直拖着。"""
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        class Ctx:
            def wrap_socket(self, raw, server_hostname=None):
                return raw
        stop = threading.Event()

        def trickle():
            b.recv(65536)
            b.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            while not stop.is_set():
                try:
                    b.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.2)
        t = threading.Thread(target=trickle, daemon=True)
        t.start()
        self.addCleanup(stop.set)
        start = time.monotonic()
        with mock.patch.object(console, "_connect", return_value=a), \
                mock.patch.object(console, "_ssl_context", return_value=Ctx()), \
                mock.patch.object(http_client.HTTPSConnection, "__init__",
                                  lambda self, h, p, timeout=None, context=None:
                                  http_client.HTTPConnection.__init__(self, h, p, timeout=timeout)):
            with self.assertRaises(OSError):
                ORIGINAL_OPEN("GET", URL + "/x", {}, None, 1.0)
        self.assertLess(time.monotonic() - start, 3.0)

    def test_deadline_covers_slow_dns(self):
        """getaddrinfo 没有超时：解析也要算进总时限。"""
        def slow(*a, **k):
            time.sleep(3)
            raise OSError("late")
        start = time.monotonic()
        with mock.patch("socket.getaddrinfo", slow):
            with self.assertRaises(OSError):
                ORIGINAL_OPEN("GET", URL + "/x", {}, None, 0.5)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_deadline_covers_every_address(self):
        """解析出多个地址时，逐个连接合起来也不超过总时限（不是每个地址各给一次完整时限）。"""
        import socket as socket_mod
        tried = []

        class SlowSock:
            def __init__(self, *a):
                self.t = None

            def settimeout(self, t):
                self.t = t

            def connect(self, addr):
                tried.append(addr)
                time.sleep(min(self.t, 0.4))
                raise socket_mod.timeout("slow")

            def close(self):
                pass
        addrs = [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("192.0.2.%d" % i, 443)) for i in range(1, 6)]
        start = time.monotonic()
        with mock.patch.object(console, "_resolve", lambda h, p, t: addrs), \
                mock.patch("socket.socket", SlowSock):
            with self.assertRaises(OSError):
                console._connect("console.example.com", 443, 1.0)
        self.assertLess(time.monotonic() - start, 1.4)
        self.assertLess(len(tried), 5)

    def test_slow_connect_leaves_no_time_for_request(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        def slow_connect(h, p, t):
            time.sleep(t + 0.1)
            return a
        with mock.patch.object(console, "_connect", slow_connect):
            with self.assertRaises(OSError):
                ORIGINAL_OPEN("GET", URL + "/x", {}, None, 0.3)
        b.settimeout(0.1)
        try:
            data = b.recv(1)
        except OSError:
            data = b""
        self.assertEqual(data, b"")  # 没有写出任何请求内容

    def test_lost_confirm_response_tells_user_to_get_device_revoked(self):
        """确认请求发出后没收到回应、重试得到 EnrollExpired：控制台可能已经登记了设备，要提示找管理员吊销。"""
        self.fake.routes[("POST", "/api/v1/enroll/confirm")] = [ConnectionError("reset"), err("EnrollExpired")]
        with self.assertRaises(ValueError) as cm:
            self.enroll(answers=("yes", "123456", "123456"))
        self.assertIn("吊销", str(cm.exception))
        self.assertIn("test-host", str(cm.exception))
        self.nothing_written()

    def test_update_401_stops_token_use_everywhere(self):
        """自动更新先发现被吊销：之后的更新和心跳都不再带令牌请求控制台。"""
        from ticketd import update
        self.enrolled_state()
        self.fake.routes[("GET", "/api/v1/updates/stable/manifest.json")] = err("DeviceRevoked", 401)
        with self.assertRaises(Exception):
            update.fetch(console.updates_base() + "/stable/manifest.json")
        self.assertIsNone(console.updates_base())
        before = len(self.fake.calls)
        res = console.send_heartbeat(self.con)
        self.assertEqual((res["sent"], res["stopped"], res["code"]), (False, True, "DeviceRevoked"))
        self.assertEqual(len(self.fake.calls), before)

    def test_reenroll_clears_stale_stopped_marker(self):
        self.enrolled_state()
        console.stopped_path().write_text("DeviceRevoked\n")
        self.enrolled_state()  # 重新入网写入新令牌
        self.assertFalse(console.stopped_path().exists())
        self.assertIsNotNone(console.updates_base())

    def test_tls_minimum_12(self):
        self.assertEqual(console._ssl_context().minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_mid_stream_ssl_error_is_network_not_cert(self):
        self.enrolled_state()
        self.fake.routes[("POST", "/api/v1/heartbeat")] = ConnectionError("TLS 连接中断：bad record mac")
        res = console.send_heartbeat(self.con)
        self.assertEqual(res["code"], "Network")

    def test_revoked_stops_updates_via_console(self):
        self.enrolled_state()
        self.assertTrue(console.updates_base())
        self.fake.routes[("POST", "/api/v1/heartbeat")] = err("DeviceRevoked", 401)
        console.send_heartbeat(self.con)
        self.assertIsNone(console.updates_base())
        self.assertEqual(update.source(), ("", False))

    def test_leave_reports_undeletable_files(self):
        self.enrolled_state()
        with mock.patch.object(console.shutil, "rmtree", lambda *a, **k: None):
            with self.assertRaises(ValueError) as cm:
                console.leave(self.con, out=self.say)
        self.assertIn("没删掉", str(cm.exception))

    def test_update_check_pins_route(self):
        """更新检查进行中退出控制台：剩下的请求失败，不会改走普通连接去请求控制台。"""
        self.enrolled_state()
        plain = []
        with mock.patch.object(update, "_open", side_effect=lambda *a, **k: plain.append(a) or (_ for _ in ()).throw(
                AssertionError("不该走普通连接"))):
            def leave_then_404(body, headers):
                console.remove_local()
                return 404, b""
            self.fake.routes[("GET", "/api/v1/updates/rekey.json")] = leave_then_404
            update._pinned.src = None
            update._pinned.src = update.source()
            try:
                self.assertIsNone(update.fetch(update.source()[0] + "/rekey.json"))
                with self.assertRaises(update.FetchError):
                    update.fetch(update.source()[0] + "/pause.json")
            finally:
                update._pinned.src = None
        self.assertEqual(plain, [])

    def test_heartbeat_reconciles_costs_at_most_hourly(self):
        console._costs_cache.update({"at": None, "bad": False})
        calls = []
        with mock.patch.object(metrics, "reconcile_costs", side_effect=lambda con: calls.append(1) or (0, [])):
            console.build_heartbeat(self.con, now=1000.0)
            console.build_heartbeat(self.con, now=1300.0)
            console.build_heartbeat(self.con, now=1000.0 + 3600)
        self.assertEqual(len(calls), 2)
