"""飞书用户身份：检查、授权链接、补发文档、失败原因中文化、只报一次、诊断包（openspec add-user-auth-link，T0107）。"""
import json
import os
import time

from tests.helpers import TicketdTestCase

NEED_AUTH = {"type": "authentication", "subtype": "token_missing",
             "message": "upload media failed: need_user_authorization (user: ou_x)",
             "hint": "run `lark-cli auth login --scope \"docs:document.media:upload\"`"}


class UserAuthTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, metrics, outbox, userauth
        self.core, self.metrics, self.outbox, self.ua = core, metrics, outbox, userauth
        self.auth_file = self.tmp / "auth-state"
        os.environ["FAKE_LARK_AUTH_FILE"] = str(self.auth_file)

    # ---------- 工具 ----------
    def set_auth(self, state):
        self.auth_file.write_text(state)

    def sent(self):
        p = self.tmp / "lark.log"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def new_ticket(self, *a, **kw):
        tid = super().new_ticket(*a, **kw)
        self.con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))
        return tid

    def ready_with(self, tid, *names):
        wd = self.core.get(self.con, tid)["workdir"]
        arts = []
        os.makedirs(wd, exist_ok=True)
        for n in names:
            p = os.path.join(wd, n)
            with open(p, "w", encoding="utf-8") as f:
                f.write("# 稿子\n正文")
            arts.append(p)
        self.core.add_report(self.con, tid, "ready", "好了", arts)
        return wd, arts

    def fail_next(self, n, err=None):
        f = self.tmp / "fail"
        f.write_text(str(n))
        os.environ["FAKE_LARK_FAIL_FILE"] = str(f)
        os.environ["FAKE_LARK_FAIL_JSON"] = json.dumps(err or NEED_AUTH)

    def stop_failing(self):
        for k in ("FAKE_LARK_FAIL_FILE", "FAKE_LARK_FAIL_JSON"):
            os.environ.pop(k, None)

    def owner_rows(self):
        return self.con.execute("SELECT * FROM outbox WHERE reply_to='@owner' ORDER BY id").fetchall()

    def link_rows(self):
        return [r for r in self.owner_rows() if "去授权" in (r["card_json"] or "")]

    # ---------- 检查（D2） ----------
    def test_check_states(self):
        for state, want in (("ok", "ok"), ("missing", "missing"), ("expired", "expired"), ("scope", "scope"),
                            ("broken", "unknown")):
            self.set_auth(state)
            self.assertEqual(self.ua.check()["state"], want, state)
        args = [a for a in self.sent() if a[:2] == ["auth", "check"]][0]
        self.assertEqual(set(args[args.index("--scope") + 1].split()), set(self.ua.CHECK_SCOPES))
        self.assertTrue(set(self.ua.CHECK_SCOPES) <= set(self.ua.SCOPES))

    def test_terminal_uses_bridge_profile_config(self):
        os.environ.pop("LARKSUITE_CLI_CONFIG_DIR", None)
        self.assertIsNone(self.ua._env())  # 没有 bridge profile 目录（非受管安装）：照原样
        d = self.tmp / "lark-channel" / "profiles" / "claude" / "lark-cli"
        d.mkdir(parents=True)
        os.environ.pop("LARK_CHANNEL_PROFILE", None)
        self.assertEqual(self.ua._env()["LARKSUITE_CLI_CONFIG_DIR"], str(d))
        os.environ["LARKSUITE_CLI_CONFIG_DIR"] = "/x"  # 守护进程：服务文件里已经有
        self.assertIsNone(self.ua._env())

    def test_unknown_never_overrides_known(self):
        self.set_auth("ok")
        self.ua.refresh(self.con)
        self.set_auth("broken")  # worker 里读不了钥匙串
        self.assertEqual(self.ua.refresh(self.con), "unknown")
        self.assertEqual(self.ua.load(self.con)["state"], "ok")
        from ticketd import cli
        self.assertTrue(dict(cli.doctor_checks(self.con, costs=False))["user_auth"].startswith("✅"))

    def test_failure_state_kept_until_reauthorized(self):
        self.set_auth("ok")  # 本地看 token 还在，但服务端已撤销：出稿报授权失败
        self.ua.mark_failure(self.con, "rc=3 " + json.dumps({"type": "authentication", "subtype": "refresh_token_revoked"}))
        self.assertEqual(self.ua.load(self.con)["state"], "expired")
        self.assertEqual(self.ua.refresh(self.con), "expired")  # token 的授权时间早于这次失败：不覆盖
        os.environ["FAKE_LARK_AUTH_GRANTED"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 60))
        self.assertEqual(self.ua.refresh(self.con), "ok")  # 之后重新授权过（终端里）：可用
        self.assertEqual(self.metrics.kv_get(self.con, self.ua.BACKFILL_KEY), "1")
        self.assertIsNone(self.metrics.kv_get(self.con, self.ua.NEED_KEY))

    def test_doctor_lines(self):
        from ticketd import cli
        self.assertIn("user_auth", cli.DOCTOR_IDS)
        line = dict(cli.doctor_checks(self.con, costs=False))["user_auth"]
        self.assertTrue(line.startswith("⚠️"))  # 还没查过：不算不通过
        self.set_auth("missing")
        self.ua.refresh(self.con)
        line = dict(cli.doctor_checks(self.con, costs=False))["user_auth"]
        self.assertTrue(line.startswith("❌"))
        self.assertIn("私信你授权链接", line)
        from ticketd import console
        self.assertIn("user_auth", console._doctor_part(self.con)["failed"])

    # ---------- 归类和中文说明（D6） ----------
    def test_classify_and_reason(self):
        raw = "rc=3 " + json.dumps(NEED_AUTH)
        self.assertEqual(self.ua.classify(raw), "auth")
        text = self.ua.reason_text(raw)
        self.assertIn("飞书用户身份没授权", text)
        self.assertIn("自动补发", text)
        self.assertNotIn("need_user_authorization", text)
        self.assertEqual(self.ua.classify("上传云空间失败：rc=3 " + json.dumps(
            {"type": "authorization", "subtype": "missing_scope", "message": "x"})), "auth")
        app = "rc=1 " + json.dumps({"type": "authorization", "subtype": "app_scope_not_applied", "message": "x"})
        self.assertEqual(self.ua.classify(app), "app")
        self.assertIn("开放平台", self.ua.reason_text(app))
        self.assertEqual(self.ua.classify("调用 lark-cli 失败：timeout"), "network")
        self.assertEqual(self.ua.classify("rc=1 " + json.dumps({"type": "authentication",
                                                                 "subtype": "refresh_server_error"})), "other")
        other = self.ua.reason_text("rc=1 " + json.dumps({"type": "api", "code": 1, "message": "file too large"}))
        self.assertIn("飞书没能导入文档（原因：file too large）", other)

    # ---------- 装好后马上发一次（D4） ----------
    def test_first_link_sent_once(self):
        self.set_auth("missing")
        self.assertEqual(self.ua.step(self.con), "link_sent")
        rows = self.link_rows()
        self.assertEqual(len(rows), 1)
        card = json.loads(rows[0]["card_json"])
        btn = [e for e in card["body"]["elements"] if e.get("tag") == "button"][0]
        self.assertTrue(btn["behaviors"][0]["default_url"].startswith("https://accounts.feishu.cn/"))
        self.assertIn("FAKE-CODE", rows[0]["text"])  # 文字兜底也带链接
        login = [a for a in self.sent() if a[:2] == ["auth", "login"]][0]
        self.assertIn("--no-wait", login)
        self.assertEqual(set(login[login.index("--scope") + 1].split()), set(self.ua.SCOPES))
        self.metrics.kv_set(self.con, self.ua.LINK_KEY, "")  # 用户没点，过期了
        for _ in range(3):
            self.assertTrue(self.ua.step(self.con).startswith("checked:"))
        self.assertEqual(len(self.link_rows()), 1)  # 装好后的那一次只发一次

    def test_link_card_delivered_to_owner(self):
        self.set_auth("missing")
        self.ua.step(self.con)
        self.outbox.flush(self.con)
        sent = [a for a in self.sent() if a[:2] == ["im", "+messages-send"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][sent[0].index("--user-id") + 1], "ou_owner")
        self.assertIn("去授权", sent[0][sent[0].index("--content") + 1])
        row = self.con.execute("SELECT * FROM outbox WHERE card_json LIKE '%去授权%'").fetchone()
        self.assertEqual((row["status"], row["reply_to"]), ("sent", "ou_owner"))

    def test_no_link_when_authorized_or_unknown(self):
        for state in ("ok", "broken"):
            self.set_auth(state)
            self.ua.step(self.con)
        self.assertEqual(self.link_rows(), [])

    def test_no_link_without_owner(self):
        os.environ["TICKETD_OWNER"] = ""
        self.set_auth("missing")
        self.ua.step(self.con)
        self.assertEqual(self.link_rows(), [])
        self.assertIsNone(self.metrics.kv_get(self.con, self.ua.FIRST_KEY))

    # ---------- 出稿失败：只报一次、中文原因、发链接 ----------
    def test_doc_failure_flow_end_to_end(self):
        self.set_auth("ok")
        self.ua.step(self.con)  # 装好时是授权的
        self.metrics.kv_set(self.con, self.ua.FIRST_KEY, "1")
        self.set_auth("expired")
        tid = self.new_ticket()
        wd, arts = self.ready_with(tid, "结果.md", "方案.md", "附录.txt")
        self.fail_next(3)
        self.outbox.flush(self.con)
        self.stop_failing()
        # 结果卡：中文、写下一步，不带英文原文
        reply = [a for a in self.sent() if a[:2] == ["im", "+messages-reply"]][-1]
        content = reply[reply.index("--content") + 1]
        self.assertIn("飞书用户身份没授权或已失效", content)
        self.assertIn("自动补发", content)
        self.assertNotIn("need_user_authorization", content)
        # 只报一次：不算媒体失败
        codes = [r["code"] for r in self.con.execute("SELECT code FROM events WHERE kind='media'")]
        self.assertEqual(codes.count("doc:auth"), 3)
        self.assertNotIn("doc:fail", codes)
        from ticketd import console
        self.assertEqual([p for e in self.con.execute("SELECT kind, code FROM events")
                          for p in [console._problem_of(e["kind"], e["code"])] if p], [])
        self.assertEqual(self.ua.load(self.con)["state"], "missing")  # 按报错子类型（token_missing）记
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0], 3)
        # 三份同时失败只发一条链接
        self.assertEqual(self.ua.step(self.con), "link_sent")
        self.assertEqual(self.ua.step(self.con), "authorized:ok")  # 用户点了同意（假 lark-cli 写回 ok）
        self.assertEqual(len(self.link_rows()), 1)
        waited = [a for a in self.sent() if "--device-code" in a]
        self.assertEqual(len(waited), 1)
        # 补发：一张单一条，回复在原结果下面，三个打开按钮
        back = self.con.execute("SELECT * FROM outbox WHERE ticket_id=? AND reply_to='om_1' AND report_id IS NULL",
                                (tid,)).fetchall()
        self.assertEqual(len(back), 1)
        card = json.loads(back[0]["card_json"])
        self.assertIn("补上 %s" % tid, card["header"]["title"]["content"])
        self.assertEqual(len([e for e in card["body"]["elements"] if e.get("tag") == "button"]), 3)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM published").fetchone()[0], 3)
        self.assertTrue(any("授权好了" in (r["text"] or "") for r in self.owner_rows()))
        # 发出去：补发卡片真的发到原消息下面
        self.outbox.flush(self.con)
        last = [a for a in self.sent() if a[:2] == ["im", "+messages-reply"]][-1]
        self.assertEqual(last[last.index("--message-id") + 1], "om_1")
        # 之后同一份稿子再汇报：用缓存，不再导入
        n = len([a for a in self.sent() if a[:2] == ["drive", "+import"]])
        self.core.add_report(self.con, tid, "ready", "又好了", arts)
        self.outbox.flush(self.con)
        self.assertEqual(len([a for a in self.sent() if a[:2] == ["drive", "+import"]]), n)

    def test_resend_after_failure_waits_30_minutes(self):
        self.set_auth("missing")
        self.ua.step(self.con)  # 装好后的一次
        self.metrics.kv_set(self.con, self.ua.LINK_KEY, "")  # 没点，过期了
        self.ua.mark_failure(self.con, "rc=3 " + json.dumps(NEED_AUTH))
        self.ua.step(self.con)
        self.assertEqual(len(self.link_rows()), 1)  # 离上次不到 30 分钟
        self.metrics.kv_set(self.con, self.ua.LAST_KEY, str(time.time() - self.ua.RESEND_AFTER - 1))
        self.assertEqual(self.ua.step(self.con), "link_sent")
        self.assertEqual(len(self.link_rows()), 2)

    def test_link_expired_keeps_state(self):
        self.set_auth("missing")
        self.ua.step(self.con)
        os.environ["FAKE_LARK_LOGIN_FAIL"] = "1"
        self.assertEqual(self.ua.step(self.con), "link_expired")
        self.assertIsNone(self.ua.live_link(self.con))
        self.assertEqual(self.ua.load(self.con)["state"], "missing")

    def test_video_auth_failure_not_media_fail(self):
        tid = self.new_ticket()
        self.ua.mark_ok(self.con)
        ok = self.outbox._user_auth_failed(self.con, "上传云空间失败：rc=3 " + json.dumps(NEED_AUTH))
        self.assertTrue(ok)
        self.assertEqual(self.ua.load(self.con)["state"], "missing")
        self.assertTrue(self.metrics.kv_get(self.con, self.ua.NEED_KEY))
        self.assertFalse(self.outbox._user_auth_failed(self.con, "调用 lark-cli 失败：timeout"))
        del tid

    # ---------- 补发：安全 ----------
    def test_backfill_skips_swapped_symlink(self):
        tid = self.new_ticket()
        wd, arts = self.ready_with(tid, "结果.md")
        self.fail_next(1)
        self.outbox.flush(self.con)
        self.stop_failing()
        secret = self.tmp / "secret.md"
        secret.write_text("# 不该被导入")
        os.unlink(arts[0])
        os.symlink(str(secret), arts[0])
        self.set_auth("ok")
        self.assertEqual(self.ua.backfill(self.con), 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0], 0)
        self.assertEqual([a for a in self.sent() if a[:2] == ["drive", "+import"]][1:], [])

    def test_backfill_still_unauthorized_keeps_rows(self):
        tid = self.new_ticket()
        self.ready_with(tid, "结果.md")
        self.fail_next(1)
        self.outbox.flush(self.con)
        self.fail_next(1)
        self.assertEqual(self.ua.backfill(self.con), 0)
        self.stop_failing()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0], 1)

    def test_terminal_authorization_triggers_backfill(self):
        tid = self.new_ticket()
        self.ready_with(tid, "结果.md")
        self.fail_next(1)
        self.outbox.flush(self.con)
        self.stop_failing()
        self.metrics.kv_set(self.con, self.ua.FIRST_KEY, "1")
        self.metrics.kv_set(self.con, self.ua.LAST_KEY, str(time.time()))  # 刚发过链接，不再发
        self.set_auth("ok")  # 同事在终端里 ticket setup --user-auth
        os.environ["FAKE_LARK_AUTH_GRANTED"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 60))
        self.ua.request_recheck(self.con)
        self.assertEqual(self.ua.step(self.con), "backfilled")
        self.assertEqual(len(self.con.execute("SELECT * FROM outbox WHERE ticket_id=? AND report_id IS NULL",
                                              (tid,)).fetchall()), 1)

    # ---------- 诊断包（D8） ----------
    def test_diag_has_reasons_but_no_device_code(self):
        tid = self.new_ticket()
        self.ready_with(tid, "结果.md")
        self.fail_next(1)
        self.outbox.flush(self.con)
        self.stop_failing()
        self.set_auth("missing")
        self.ua.step(self.con)
        code = json.loads(self.metrics.kv_get(self.con, self.ua.LINK_KEY))["code"]
        from ticketd import diag
        text = diag.build(self.con)
        self.assertIn("飞书用户身份和最近的上传失败", text)
        self.assertIn("need_user_authorization", text)  # 告警日志里的原文
        self.assertIn("结果.md", text)
        self.assertIn("有一条还没过期", text)
        self.assertIn("等授权后补发的文档：1 份", text)
        self.assertNotIn(code, text)

    # ---------- 审查后补的 ----------
    def test_partial_grant_counts_as_authorized(self):
        self.set_auth("missing")
        self.ua.step(self.con)
        os.environ["FAKE_LARK_LOGIN_PARTIAL"] = "1"
        self.assertEqual(self.ua.step(self.con), "authorized:scope")
        self.assertTrue(any("还缺" in (r["text"] or "") for r in self.owner_rows()))
        self.assertIsNone(self.ua.live_link(self.con))

    def test_use_ok_does_not_flip_state_and_backfill_flag_clears(self):
        self.set_auth("scope")
        self.ua.refresh(self.con)
        self.ua.mark_ok(self.con)  # 文档导入成功了，但缺的是上传视频的权限
        self.assertEqual(self.ua.load(self.con)["state"], "scope")
        self.assertEqual(self.metrics.kv_get(self.con, self.ua.RECHECK_KEY), "1")
        self.metrics.kv_set(self.con, self.ua.BACKFILL_KEY, "1")
        self.metrics.kv_set(self.con, self.ua.FIRST_KEY, "1")
        self.ua.step(self.con)
        self.assertIsNone(self.metrics.kv_get(self.con, self.ua.BACKFILL_KEY))
        self.assertIsNone(self.metrics.kv_get(self.con, self.ua.RECHECK_KEY))

    def test_use_ok_after_failure_clears_on_recheck(self):
        self.set_auth("ok")
        self.ua.mark_failure(self.con, "rc=3 " + json.dumps(NEED_AUTH))
        time.sleep(0.01)
        self.ua.mark_ok(self.con)  # 之后一次导入又成功了：现查是 ok 就转回 ok
        self.ua.step(self.con)
        self.assertEqual(self.ua.load(self.con)["state"], "ok")

    def test_owner_invalid_alerts_once_and_waits(self):
        os.environ["TICKETD_OWNER"] = ""
        self.set_auth("missing")
        self.ua.step(self.con)
        self.ua.mark_failure(self.con, "rc=3 " + json.dumps(NEED_AUTH))
        self.ua.step(self.con)
        self.assertTrue(self.metrics.kv_get(self.con, self.ua.LAST_KEY))
        alerts = (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8")
        self.assertEqual(alerts.count("TICKETD_OWNER 无效"), 1)
        lane = self.ua.Lane(clock=lambda: time.time(), worker=lambda: None)
        lane.next_check = time.time() + 999
        self.assertFalse(lane.due(self.con, time.time()))  # 30 分钟内不再试

    def test_uploaded_but_no_link_is_not_auth(self):
        info = "已上传（file_token=boxcn_x）但拿不到链接：rc=3 " + json.dumps(
            {"type": "authorization", "subtype": "missing_scope", "message": "x"})
        tid = self.new_ticket()
        wd = self.core.get(self.con, tid)["workdir"]
        os.makedirs(wd, exist_ok=True)
        from ticketd import templates
        card = {"body": {"elements": [{templates.VIDEO_KEY: "v.mp4"}]}}
        self.outbox._resolve_videos(card, wd, tid, drive_uploader=lambda *a: (False, info),
                                    uploader=lambda *a: (False, "x", True), con=self.con)
        self.assertEqual(self.ua.load(self.con), {})
        self.assertIn("video:fail", [r["code"] for r in self.con.execute("SELECT code FROM events")])

    def test_backfill_network_error_keeps_rows(self):
        tid = self.new_ticket()
        self.ready_with(tid, "结果.md")
        self.fail_next(1)
        self.outbox.flush(self.con)
        self.fail_next(1, {"type": "network", "message": "timeout"})
        self.assertEqual(self.ua.backfill(self.con), 0)
        self.stop_failing()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0], 1)

    def test_backfill_overwrites_existing_doc(self):
        tid = self.new_ticket()
        wd, arts = self.ready_with(tid, "结果.md")
        self.outbox.flush(self.con)  # 第一版导入成功
        with open(arts[0], "w", encoding="utf-8") as f:
            f.write("# 第二版")
        os.utime(arts[0], (time.time() + 5, time.time() + 5))
        self.core.add_report(self.con, tid, "ready", "改好了", arts)
        self.fail_next(2)  # 覆盖原文档、改为另导入，都因为没授权失败
        self.outbox.flush(self.con)
        self.stop_failing()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0], 1)
        n = len([a for a in self.sent() if a[:2] == ["drive", "+import"]])
        self.assertEqual(self.ua.backfill(self.con), 1)
        self.assertEqual(len([a for a in self.sent() if a[:2] == ["drive", "+import"]]), n)  # 没有另导入
        self.assertEqual(self.sent()[-1][:2], ["docs", "+update"])

    def test_stop_keeps_link_for_next_daemon(self):
        self.set_auth("missing")
        self.ua.step(self.con)
        self.ua._waiting["stopped"] = False
        orig = self.ua.wait_link

        def interrupted(link, now=None):
            self.ua.stop()
            return False
        self.ua.wait_link = interrupted
        try:
            self.assertEqual(self.ua.step(self.con), "stopped")
        finally:
            self.ua.wait_link = orig
        self.assertIsNotNone(self.ua.live_link(self.con))

    # ---------- 守护进程节流 ----------
    def test_lane_throttle(self):
        runs = []
        now = [1000.0]
        lane = self.ua.Lane(clock=lambda: now[0], worker=lambda: runs.append(now[0]))
        self.assertFalse(lane.tick(self.con))  # 刚启动先等一会儿
        now[0] += 21
        self.assertTrue(lane.tick(self.con))
        lane.thread.join()
        now[0] += 1
        self.assertFalse(lane.tick(self.con))  # 下一次定时检查还没到
        self.ua.request_recheck(self.con)
        self.assertFalse(lane.tick(self.con))  # 刚做完：至少隔 MIN_GAP
        now[0] += self.ua.MIN_GAP
        self.assertTrue(lane.tick(self.con))
        lane.thread.join()
        self.assertEqual(len(runs), 2)
