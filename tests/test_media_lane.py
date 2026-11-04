"""媒体上传与主循环解耦、SQLite 异常分类。"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time

from helpers import REPO, TicketdTestCase
import test_send_robustness as _srt

MP4, PNG, find_all = _srt.MP4, _srt.PNG, _srt.find_all


class _Base(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, db, outbox
        self.core, self.db, self.outbox = core, db, outbox
        self._orig = (outbox.upload_image, outbox.upload_video_drive)
        self.texts, self.cards = [], []

    def tearDown(self):
        self.outbox.upload_image, self.outbox.upload_video_drive = self._orig
        super().tearDown()

    def alerts(self):
        from ticketd import config
        p = config.logs_dir() / "alerts.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""


    def ticket(self):
        """同一个用例里可以建多张单（每张绑不同的源消息）。"""
        self._n = getattr(self, "_n", 0) + 1
        tid = self.new_ticket(message="om_%d" % self._n)
        self.con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))
        wd = self.con.execute("SELECT workdir FROM tickets WHERE id=?", (tid,)).fetchone()["workdir"]
        os.makedirs(wd, exist_ok=True)
        return tid, wd


for _n in ("write", "rows", "sender", "ok_card"):
    setattr(_Base, _n, getattr(_srt.SendRobustnessTest, _n))


# ---------- P2：SQLite 异常分类 ----------
class IsBusyTest(_Base):
    def test_only_busy_and_locked_are_retryable(self):
        for msg in ("database is locked", "database table is locked", "database table is locked: outbox",
                    "database schema is locked: main", "database is busy"):
            self.assertTrue(self.db.is_busy(sqlite3.OperationalError(msg)), msg)
        for msg in ("disk I/O error", "no such column: media_json", "no such table: drive_links",
                    "attempt to write a readonly database", "database or disk is full", "unable to open database file"):
            self.assertFalse(self.db.is_busy(sqlite3.OperationalError(msg)), msg)
        self.assertFalse(self.db.is_busy(sqlite3.DatabaseError("database disk image is malformed")))
        self.assertFalse(self.db.is_busy(RuntimeError("database is locked")))

    def test_real_lock_from_sqlite_is_recognized(self):
        other = self.db.connect()
        self.addCleanup(other.close)
        other.execute("PRAGMA busy_timeout=0")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError) as cm:
                other.execute("BEGIN IMMEDIATE")
        finally:
            self.con.execute("ROLLBACK")
        self.assertTrue(self.db.is_busy(cm.exception), repr(cm.exception))

    def test_real_schema_error_from_sqlite_is_not_busy(self):
        with self.assertRaises(sqlite3.OperationalError) as cm:
            self.con.execute("SELECT no_such_col FROM outbox")
        self.assertFalse(self.db.is_busy(cm.exception))


class DbFaultTest(_Base):
    def _fail_card(self, msg):
        def broken(*a):
            raise sqlite3.OperationalError(msg)
        return broken

    def _fake_clock(self):
        clock = {"t": 1000.0}
        self.outbox._clock = lambda: clock["t"]
        self.addCleanup(setattr, self.outbox, "_clock", time.monotonic)
        return clock

    def test_non_lock_error_backs_off_without_24h_retry_or_instant_poison(self):
        clock = self._fake_clock()
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        broken = self._fail_card("disk I/O error")
        self.outbox.flush(self.con, sender=self.sender, card_sender=broken)
        r = self.rows()[0]
        self.assertEqual(r["status"], "pending")  # 一次 I/O 错误可能是暂时的：不马上把卡片永久换成文字
        self.assertNotEqual(r["card_json"], "")
        self.assertGreater(r["next_at"], self.db.now())
        self.assertIn("数据库故障", self.alerts())
        self.assertEqual(self.texts, [])
        for _ in range(3):  # 反复出错，但这一行还不到 10 分钟：继续退避
            clock["t"] += 1000
            self.con.execute("UPDATE outbox SET next_at=0")
            self.outbox.flush(self.con, sender=self.sender, card_sender=broken)
        self.assertEqual((self.rows()[0]["status"], self.texts), ("pending", []))
        # 等够 10 分钟仍在出错：按毒丸处理，结果以文字送到（不是原来的 24 小时）
        self.con.execute("UPDATE outbox SET next_at=0, created_at=created_at-700")
        clock["t"] += 1000
        self.outbox.flush(self.con, sender=self.sender, card_sender=broken)
        self.assertEqual(self.rows()[0]["status"], "sent")
        self.assertEqual(len(self.texts), 1)

    def test_backoff_holds_in_memory_even_if_next_at_is_reset(self):
        clock = self._fake_clock()
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        calls = []

        def broken(*a):
            calls.append(1)
            raise sqlite3.OperationalError("disk I/O error")

        self.outbox.flush(self.con, sender=self.sender, card_sender=broken)
        self.con.execute("UPDATE outbox SET next_at=0")
        self.outbox.flush(self.con, sender=self.sender, card_sender=broken)
        self.assertEqual(len(calls), 1)  # 库里的 next_at 可能写不进去：内存里的退避照样生效
        clock["t"] += 31
        self.outbox.flush(self.con, sender=self.sender, card_sender=broken)
        self.assertEqual(len(calls), 2)

    def test_non_lock_error_does_not_stall_other_tickets(self):
        a, _ = self.ticket()
        b, _ = self.ticket()
        self.core.add_report(self.con, a, "ready", "A 做好了", [])
        self.core.enqueue_message(self.con, b, "B 的消息")
        orig = self.outbox._send_row

        def send_row(r, sender, card_sender, con, **kw):
            if r["ticket_id"] == a and r["card_json"]:
                raise sqlite3.OperationalError("no such column: card_json")
            return orig(r, sender, card_sender, con, **kw)

        self.outbox._send_row = send_row
        self.addCleanup(setattr, self.outbox, "_send_row", orig)
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card)
        status = {r["ticket_id"]: r["status"] for r in self.rows()}
        self.assertEqual(status[b], "sent")  # 原来这里会 break，整个队列每 30 秒重撞一次、最长 24 小时
        self.assertEqual(status[a], "pending")  # A 自己退避
        self.assertEqual([t for t, _ in self.texts], ["B 的消息"])

    def _no_doc_links(self):
        # 只读库上稿子缓存（published）也写不进，会在发送前就按故障退避；这里单测「发出去之后写不进库」这条路径
        orig = self.outbox._doc_links
        self.outbox._doc_links = lambda con, r, allow_import=True: []
        self.addCleanup(setattr, self.outbox, "_doc_links", orig)

    def test_unwritable_db_sends_nothing_before_doc_cache(self):
        tid, _ = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        from ticketd import config
        ro = sqlite3.connect("file:%s?mode=ro" % config.db_path(), uri=True, isolation_level=None)
        ro.row_factory = sqlite3.Row
        self.addCleanup(ro.close)
        for _ in range(4):
            self.outbox.flush(ro, sender=self.sender, card_sender=self.ok_card)
        self.assertEqual((self.cards, self.texts), ([], []))  # 库写不进：发了也记不下来，不发
        self.assertIn("数据库故障", self.alerts())

    def test_unwritable_db_does_not_resend_every_round(self):
        self._no_doc_links()
        a, _ = self.ticket()
        b, _ = self.ticket()
        self.core.add_report(self.con, a, "ready", "A 做好了", [])
        self.core.enqueue_message(self.con, b, "B 的消息")
        from ticketd import config
        ro = sqlite3.connect("file:%s?mode=ro" % config.db_path(), uri=True, isolation_level=None)
        ro.row_factory = sqlite3.Row
        self.addCleanup(ro.close)
        for _ in range(4):
            self.outbox.flush(ro, sender=self.sender, card_sender=self.ok_card)
        self.assertEqual(len(self.cards), 1)  # 发出去了但记不进库：退避，不是每轮重发一遍
        self.assertEqual(len(self.texts), 1)
        self.assertIn("发送后写库失败", self.alerts())

    def test_unwritable_db_poison_text_not_repeated(self):
        self._no_doc_links()
        tid, _ = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        from ticketd import config
        ro = sqlite3.connect("file:%s?mode=ro" % config.db_path(), uri=True, isolation_level=None)
        ro.row_factory = sqlite3.Row
        self.addCleanup(ro.close)

        def boom(*a):
            raise RuntimeError("boom")

        for _ in range(4):
            self.outbox.flush(ro, sender=self.sender, card_sender=boom)
        self.assertEqual(len(self.texts), 1)  # 毒丸的文字兜底发了一次；写不进库时不每轮再发

    def test_lock_still_backs_off_and_keeps_card(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        self.outbox.flush(self.con, sender=self.sender, card_sender=self._fail_card("database is locked"))
        r = self.rows()[0]
        self.assertEqual(r["status"], "pending")
        self.assertGreater(r["next_at"], self.db.now())
        self.assertNotEqual(r["card_json"], "")
        self.assertEqual(self.texts, [])
        self.assertIn("库被占用", self.alerts())


class MediaLaneOnlyPreparesTest(_Base):
    """结果卡的发送（含覆盖状态卡）只在主循环里做：媒体通道只上传、写回，不发卡、不 PATCH 状态卡，
    这样不会和 card.refresh 的 PATCH 互相覆盖。"""

    def test_media_lane_prepares_then_fast_lane_sends(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "a.png"), PNG)
        self.core.add_report(self.con, tid, "ready", "图好了", ["a.png"])
        self.outbox.upload_image = lambda p: (True, "img_1", False)
        patches = []
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card,
                          card_patcher=lambda *a: (patches.append(a), (True, "", False))[1],
                          lane=self.outbox.LANE_MEDIA)
        r = self.rows()[0]
        self.assertEqual((self.cards, self.texts, patches), ([], [], []))
        self.assertEqual((r["status"], r["attempts"], r["next_at"]), ("pending", 0, 0))
        self.assertNotIn("ticketd_upload", r["card_json"])
        self.assertFalse(self.outbox.needs_media(r, self.con))
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual(len(self.cards), 1)
        self.assertEqual([i["img_key"] for i in find_all(self.cards[0], "img")], ["img_1"])

    def test_doc_import_runs_in_media_lane_not_main_loop(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "方案.md"), "# 方案\n内容".encode("utf-8"))
        self.core.add_report(self.con, tid, "ready", "稿子好了", ["方案.md"])
        log = self.tmp / "lark.log"

        def imports():
            return [x for x in log.read_text(encoding="utf-8").splitlines() if "+import" in x] if log.exists() else []

        self.assertTrue(self.outbox.needs_media(self.rows()[0], self.con))
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual((imports(), self.cards), ([], []))  # 主循环不导入，也不先发没有稿子按钮的卡
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_MEDIA)
        self.assertEqual((len(imports()), self.cards), (1, []))
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual(len(imports()), 1)  # 用缓存，不再导入
        self.assertIn("example.feishu.cn/docx/", json.dumps(self.cards[0], ensure_ascii=False))

    def test_failed_doc_import_does_not_pin_row_to_media_lane(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "方案.md"), "# 方案".encode("utf-8"))
        self.core.add_report(self.con, tid, "ready", "稿子好了", ["方案.md"])
        from ticketd import publish
        orig = publish._import
        publish._import = lambda p, n: (_ for _ in ()).throw(RuntimeError("调用 lark-cli 失败：boom"))
        self.addCleanup(setattr, publish, "_import", orig)
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_MEDIA)
        self.assertFalse(self.outbox.needs_media(self.rows()[0], self.con))
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual(len(self.cards), 1)  # 导入失败只是没有按钮，结果照常送达


# ---------- P1：快、慢两条通道 ----------
class LaneTest(_Base):
    def _media_row(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "a.png"), PNG)
        self.core.add_report(self.con, tid, "ready", "图好了", ["a.png"])
        return tid

    def test_fast_lane_skips_media_rows_but_delivers_others(self):
        uploads = []
        self.outbox.upload_image = lambda p: (uploads.append(p), (True, "img_1", False))[1]
        a = self._media_row()
        self.core.enqueue_message(self.con, a, "A 后面一条")
        b, _ = self.ticket()
        self.core.enqueue_message(self.con, b, "B 的消息")
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual(uploads, [])  # 主循环不碰上传
        self.assertEqual([t for t, _ in self.texts], ["B 的消息"])  # 别的工单照常发；A 后面那条仍排在卡片后
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_MEDIA)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(self.cards, [])  # 媒体通道只准备，不发
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_MEDIA)
        self.assertEqual((len(uploads), len(self.texts)), (1, 1))  # 准备好的行和纯文字行媒体通道都不管
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual(len(self.cards), 1)
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual([t for t, _ in self.texts], ["B 的消息", "A 后面一条"])

    def test_video_message_row_goes_to_media_lane(self):
        tid, wd = self.ticket()
        self.con.execute("INSERT INTO outbox(ticket_id, reply_to, text, media_json, created_at) VALUES(?,?,?,?,?)",
                         (tid, "om_1", "兜底", json.dumps({"video": "v.mp4"}), self.db.now()))
        r = self.rows()[0]
        self.assertTrue(self.outbox.needs_media(r))
        self.con.execute("UPDATE outbox SET media_json=''")
        self.assertFalse(self.outbox.needs_media(self.rows()[0]))

    def test_claimed_row_is_skipped(self):
        b, _ = self.ticket()
        self.core.enqueue_message(self.con, b, "B")
        rid = self.rows()[0]["id"]
        self.assertTrue(self.outbox._claim(rid))
        try:
            self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card)
            self.assertEqual(self.texts, [])
        finally:
            self.outbox._release(rid)
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card)
        self.assertEqual(len(self.texts), 1)

    def test_row_sent_by_other_lane_after_fetch_is_not_resent(self):
        a, _ = self.ticket()
        b, _ = self.ticket()
        self.core.enqueue_message(self.con, a, "A")
        self.core.enqueue_message(self.con, b, "B")

        def sender(to, text, idem):
            if text == "A":  # 发 A 的同时，另一条通道把 B 发掉了（本轮的行列表里 B 还是 pending）
                self.con.execute("UPDATE outbox SET status='sent' WHERE ticket_id=?", (b,))
            return self.sender(to, text, idem)

        self.outbox.flush(self.con, sender=sender, card_sender=self.ok_card)
        self.assertEqual([t for t, _ in self.texts], ["A"])

    def test_limit_counts_only_rows_in_lane(self):
        for _ in range(6):
            self._media_row()
        b, _ = self.ticket()
        self.core.enqueue_message(self.con, b, "B")
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual([t for t, _ in self.texts], ["B"])  # 前面 6 张待上传的卡不占快通道的名额


class MediaLaneThreadTest(_Base):
    """真线程：一次上传卡住时，主循环这边的投递照常进行。"""

    def setUp(self):
        super().setUp()
        self.lanes = []

    def tearDown(self):
        # 必须在 super().tearDown() 恢复环境变量、删临时目录之前停掉线程：否则它可能连上真实的 ~/.ticketd
        for lane in self.lanes:
            lane.stop(timeout=10)
            self.assertFalse(lane.thread is not None and lane.thread.is_alive(), "媒体通道线程没停下")
        super().tearDown()

    def lane(self):
        from ticketd.daemon import MediaLane
        lane = MediaLane(interval=0.05)
        self.lanes.append(lane)
        return lane

    def test_slow_upload_does_not_block_fast_lane(self):
        a, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        started, release = threading.Event(), threading.Event()

        def slow_drive(video, workdir, ticket_id):
            started.set()
            release.wait(30)
            return True, "https://a.feishu.cn/file/slow"

        self.outbox.upload_video_drive = slow_drive
        self.core.add_report(self.con, a, "ready", "视频好了", ["v.mp4"])
        lane = self.lane()
        lane.ensure()
        try:
            self.assertTrue(started.wait(10), "媒体通道没有开始上传")
            b, _ = self.ticket()
            self.core.enqueue_message(self.con, b, "B 的消息")
            t0 = time.monotonic()
            self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
            self.assertLess(time.monotonic() - t0, 2)
            self.assertEqual([t for t, _ in self.texts], ["B 的消息"])  # 上传还卡着，B 已经送达
            self.assertEqual(self.rows()[0]["status"], "pending")
        finally:
            release.set()
        end = time.time() + 10
        while time.time() < end and "ticketd_video" in self.rows()[0]["card_json"]:
            time.sleep(0.05)
        r = self.rows()[0]
        self.assertEqual(r["status"], "pending")  # 媒体通道只把链接写回，发送留给主循环
        self.assertIn("https://a.feishu.cn/file/slow", r["card_json"])
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)
        self.assertEqual(self.rows()[0]["status"], "sent")
        lane.stop()
        self.assertFalse(lane.thread.is_alive())

    def test_lane_restarts_after_thread_death(self):
        lane = self.lane()
        lane.ensure()
        first = lane.thread
        lane.stopping.set()
        first.join(5)
        lane.stopping.clear()
        lane.ensure()
        self.assertIsNot(lane.thread, first)
        self.assertTrue(lane.thread.is_alive())
        self.assertIn("媒体通道线程意外退出", self.alerts())


class DaemonSlowUploadTest(_Base):
    """真守护进程 + fake lark：云空间上传 8 秒期间，另一张新单照常派发、发状态卡和结果卡，心跳照常。"""

    def test_daemon_keeps_running_during_slow_drive_upload(self):
        from ticketd import config
        a, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.con.execute("UPDATE inputs SET status='done' WHERE ticket_id=?", (a,))
        self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (a,))
        self.core.add_report(self.con, a, "ready", "视频好了", ["v.mp4"])
        log = self.tmp / "lark.log"
        env = dict(os.environ, FAKE_LARK_DRIVE_SLEEP="8")
        proc = subprocess.Popen([str(REPO / "bin" / "ticketd")], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def calls():
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

        def status(tid):
            r = self.con.execute("SELECT status FROM outbox WHERE ticket_id=? AND report_id IS NOT NULL"
                                 " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            return r["status"] if r else None

        try:
            end = time.time() + 10
            while time.time() < end and not any(c[:2] == ["drive", "+upload"] for c in calls()):
                time.sleep(0.1)
            self.assertTrue(any(c[:2] == ["drive", "+upload"] for c in calls()), "没有开始云空间上传")
            hb = config.heartbeat_path()
            hb0 = hb.stat().st_mtime if hb.exists() else 0  # 旧代码在这里还没写过心跳：第一轮就卡在上传上
            b = self.new_ticket(message="om_new")  # 上传进行中派一张新单
            end = time.time() + 7
            while time.time() < end:
                if ["drive-upload-finished"] in calls() or status(b) == "sent":
                    break
                time.sleep(0.1)
            cs = calls()
            self.assertNotIn(["drive-upload-finished"], cs, "上传结束前新单的结果没发出来：主循环被上传卡住了")
            self.assertEqual(status(b), "sent")  # 新单：派发、发状态卡、结果送达，都在上传进行中完成
            self.assertTrue(any("interactive" in c for c in cs))  # 新单的状态卡
            self.assertEqual(status(a), "pending")
            self.assertGreater(hb.stat().st_mtime, hb0)  # 心跳没停
            end = time.time() + 15
            while time.time() < end and status(a) != "sent":
                time.sleep(0.1)
            self.assertIn(["drive-upload-finished"], calls())
            self.assertEqual(status(a), "sent")  # 上传完，A 的视频卡也送出了
            card_a = self.con.execute("SELECT card_json FROM outbox WHERE ticket_id=? AND report_id IS NOT NULL",
                                      (a,)).fetchone()["card_json"]
            self.assertIn("example.feishu.cn/file/", card_a)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=20)
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
