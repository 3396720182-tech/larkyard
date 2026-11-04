"""媒体通道任务池：不同工单的媒体并行准备，视频单独限流，同一工单仍按顺序，卡死能被发现。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time

from helpers import REPO
from test_media_lane import MP4, PNG, _Base


class MediaPoolTest(_Base):
    """真线程：MediaLane 是有并发上限的任务池，视频另有上限；同一工单仍一次只处理一行。"""

    def setUp(self):
        super().setUp()
        self.lanes, self.gates = [], []

    def tearDown(self):
        for g in self.gates:  # 先放行还卡着的上传，再停线程（要在 super().tearDown() 删临时目录之前）
            g.set()
        for lane in self.lanes:
            lane.stop(timeout=10)
            self.assertFalse(lane.thread is not None and lane.thread.is_alive(), "调度线程没停下")
            self.assertEqual(lane.busy(), [], "还有任务没结束")
        super().tearDown()

    def lane(self, **kw):
        from ticketd.daemon import MediaLane
        kw.setdefault("interval", 0.05)
        lane = MediaLane(sender=self.sender, **kw)
        self.lanes.append(lane)
        return lane

    def gate(self):
        g = threading.Event()
        self.gates.append(g)
        return g

    def until(self, cond, timeout=10, msg="条件没有满足"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.02)
        self.fail(msg)

    def row(self, tid):
        return self.con.execute("SELECT * FROM outbox WHERE ticket_id=? ORDER BY id LIMIT 1", (tid,)).fetchone()

    def ready(self, tid) -> bool:
        """这一单最早那行已经不用再准备媒体了。"""
        return not self.outbox.needs_media(self.row(tid), self.con)

    def video_ticket(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.core.add_report(self.con, tid, "ready", "视频好了", ["v.mp4"])
        return tid

    def image_ticket(self):
        tid, wd = self.ticket()
        for n in ("a.png", "b.png", "c.png"):
            self.write(os.path.join(wd, n), PNG)
        self.core.add_report(self.con, tid, "ready", "图好了", ["a.png", "b.png", "c.png"])
        return tid

    def doc_ticket(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "方案.md"), "# 方案\n内容".encode("utf-8"))
        self.core.add_report(self.con, tid, "ready", "稿子好了", ["方案.md"])
        return tid

    def slow_drive(self, gate, started):
        def drive(video, workdir, ticket_id):
            started.append(ticket_id)
            gate.wait(30)
            return True, "https://a.feishu.cn/file/" + ticket_id
        return drive

    def fast_flush(self):
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, lane=self.outbox.LANE_FAST)

    def test_other_tickets_media_not_queued_behind_slow_video(self):
        """外部审查的场景：A 传大视频期间，B 的三张图、C 的稿子照常准备好并送达。"""
        gate, started = self.gate(), []
        self.outbox.upload_video_drive = self.slow_drive(gate, started)
        self.outbox.upload_image = lambda p: (True, "img_ok", False)
        a = self.video_ticket()
        b = self.image_ticket()
        c = self.doc_ticket()
        self.lane(workers=2, video_workers=1).ensure()
        self.until(lambda: started == [a], msg="A 的视频没开始上传")
        self.until(lambda: self.ready(b) and self.ready(c), msg="A 的视频还在传，B / C 的媒体排在后面没准备")
        self.fast_flush()
        status = {r["ticket_id"]: r["status"] for r in self.rows()}
        self.assertEqual((status[a], status[b], status[c]), ("pending", "sent", "sent"))
        sent = json.dumps(self.cards, ensure_ascii=False)
        self.assertEqual(sent.count('"img_ok"'), 3)  # B 的三张图都在卡里
        self.assertIn("example.feishu.cn/docx/", sent)  # C 的稿子按钮
        gate.set()
        self.until(lambda: self.ready(a), msg="A 放行后没准备好")
        self.fast_flush()
        self.assertEqual(self.row(a)["status"], "sent")
        self.assertIn("https://a.feishu.cn/file/" + a, json.dumps(self.cards[-1], ensure_ascii=False))

    def test_video_uploads_have_their_own_limit(self):
        gate, started = self.gate(), []
        self.outbox.upload_video_drive = self.slow_drive(gate, started)
        self.outbox.upload_image = lambda p: (True, "img_1", False)
        a = self.video_ticket()
        b = self.video_ticket()
        c = self.image_ticket()
        lane = self.lane(workers=3, video_workers=1)
        lane.ensure()
        self.until(lambda: self.ready(c), msg="C 的图没准备")
        time.sleep(0.3)  # 有空闲名额也不开第二个视频
        self.assertEqual(started, [a])
        self.assertEqual([t["ticket"] for t in lane.busy()], [a])
        gate.set()
        self.until(lambda: started == [a, b], msg="A 传完后 B 没接着传")

    def test_two_video_slots_when_configured(self):
        gate, started = self.gate(), []
        self.outbox.upload_video_drive = self.slow_drive(gate, started)
        a = self.video_ticket()
        b = self.video_ticket()
        self.lane(workers=2, video_workers=2).ensure()
        self.until(lambda: sorted(started) == sorted([a, b]), msg="两个视频名额没同时用上")

    def test_same_ticket_stays_in_order(self):
        """A 的结果卡还在传图时，A 后面的视频消息不能先发（哪怕有空闲名额）；B 不受影响。"""
        gate, uploads, videos = self.gate(), [], []

        def slow_image(p):
            uploads.append(p)
            gate.wait(30)
            return True, "img_a", False

        self.outbox.upload_image = slow_image
        self.outbox.upload_video_drive = lambda v, w, t: (True, "https://a.feishu.cn/file/b")
        orig_send_video = self.outbox.send_video
        self.outbox.send_video = lambda to, v, wd, idem: (videos.append(v), (True, "om_v", False))[1]
        self.addCleanup(setattr, self.outbox, "send_video", orig_send_video)
        a, wd = self.ticket()
        self.write(os.path.join(wd, "a.png"), PNG)
        self.core.add_report(self.con, a, "ready", "图好了", ["a.png"])
        self.con.execute("INSERT INTO outbox(ticket_id, reply_to, text, media_json, created_at) VALUES(?,?,?,?,?)",
                         (a, "om_1", "兜底", json.dumps({"video": "v.mp4"}), self.db.now()))
        b = self.video_ticket()
        lane = self.lane(workers=3, video_workers=2)
        lane.ensure()
        self.until(lambda: len(uploads) == 1, msg="A 的图没开始传")
        self.until(lambda: self.ready(b), msg="B 没准备")
        time.sleep(0.3)
        self.assertEqual(videos, [])  # A 的视频消息排在 A 的结果卡后面
        self.assertEqual([t["ticket"] for t in lane.busy()], [a])
        gate.set()
        self.until(lambda: self.ready(a), msg="A 的卡没准备好")
        time.sleep(0.2)
        self.assertEqual(videos, [])  # 卡准备好了但还没发：视频消息仍然等着
        self.fast_flush()
        self.until(lambda: videos == ["v.mp4"], msg="A 的卡发出后视频消息没发")

    def test_stuck_task_gives_up_its_slot_but_keeps_the_row(self):
        gate, uploads = self.gate(), []

        def upload(p):
            uploads.append(threading.current_thread().name)
            if len(uploads) == 1:
                gate.wait(30)  # 第一次上传卡住
            return True, "img_%d" % len(uploads), False

        self.outbox.upload_image = upload
        a, wa = self.ticket()
        self.write(os.path.join(wa, "a.png"), PNG)
        self.core.add_report(self.con, a, "ready", "图好了", ["a.png"])
        b = self.image_ticket()
        lane = self.lane(workers=1, stuck_after=0.3)
        lane.ensure()
        self.until(lambda: "可能卡死" in self.alerts(), msg="卡住的任务没告警")
        self.until(lambda: self.ready(b), msg="名额没让出来，B 一直在等")
        self.assertFalse(self.ready(a))
        self.assertEqual(self.alerts().count("可能卡死"), 1)
        self.assertEqual(uploads.count("ticketd-media-%d" % self.row(a)["id"]), 1)  # 卡住的行没被重复处理
        gate.set()
        self.until(lambda: self.ready(a), msg="A 放行后没准备好")
        self.until(lambda: "结束了" in self.alerts(), msg="卡住的任务结束后没告知")

    def _dispatch_once(self, lane):
        lane.con = self.db.connect()
        try:
            lane._dispatch()
            self.until(lambda: lane.busy() == [], msg="任务没结束")
        finally:
            lane.con.close()
            lane.con = None

    def test_round_robin_lets_others_go_first(self):
        """一行用完上传预算回到队列后，排在还没轮到的行后面（原来总是 id 小的先上，多视频卡会一直占着通道）。"""
        self.outbox.upload_image = lambda p: (True, "img", False)
        a = self.image_ticket()
        b = self.image_ticket()
        lane = self.lane(workers=1, interval=60)
        lane.last_run[self.row(a)["id"]] = time.monotonic()  # A 刚被处理过
        self._dispatch_once(lane)
        self.assertTrue(self.ready(b))
        self.assertFalse(self.ready(a))

    def test_round_robin_record_kept_while_row_runs(self):
        gate = self.gate()
        self.outbox.upload_image = lambda p: (gate.wait(30), (True, "img", False))[1]
        a = self.image_ticket()
        lane = self.lane(workers=2, interval=60)
        lane.con = self.db.connect()
        try:
            lane._dispatch()
            self.until(lambda: len(lane.busy()) == 1, msg="A 没开始")
            lane._dispatch()  # 还有空名额：这一轮会清理轮转记录，A 正在跑、不在候选里，也不能被清掉
            self.assertIn(self.row(a)["id"], lane.last_run)
            gate.set()
            self.until(lambda: lane.busy() == [], msg="任务没结束")
        finally:
            lane.con.close()
            lane.con = None

    def test_round_robin_record_kept_while_row_backs_off(self):
        self.outbox.upload_image = lambda p: (False, "调用 lark-cli 失败：timeout", False)
        a = self.image_ticket()
        lane = self.lane(workers=2, interval=60)
        self._dispatch_once(lane)  # A 网络失败，进入退避：不在候选里
        self.assertGreater(self.row(a)["next_at"], self.db.now())
        lane.hold_until = 0
        self._dispatch_once(lane)
        self.assertIn(self.row(a)["id"], lane.last_run)  # 退避结束回到队列时仍排在没轮到的行后面

    def test_network_failure_holds_new_dispatch_briefly(self):
        self.outbox.upload_image = lambda p: (False, "调用 lark-cli 失败：timeout", False)
        a = self.image_ticket()
        lane = self.lane(workers=2, interval=60)
        self._dispatch_once(lane)
        r = self.row(a)
        self.assertEqual((r["status"], r["attempts"]), ("pending", 1))  # 这一行按网络失败退避
        self.assertGreater(lane.hold_until, time.monotonic())  # 本轮不再派新任务

    def test_dispatcher_stall_is_reported(self):
        lane = self.lane(interval=60, stall_after=0.2)
        first = threading.Event()
        lane._dispatch = first.set  # 调度线程先写 beat 再调 _dispatch：等它转完第一圈，免得它覆盖下面造的旧值
        lane.ensure()
        self.assertTrue(first.wait(10), "调度线程没转第一圈")
        lane.beat = time.monotonic() - 10  # 调度线程很久没转圈（interval=60，它自己不会刷新）
        lane.ensure()
        lane.ensure()
        self.assertEqual(self.alerts().count("调度线程 10 秒没有响应"), 1)  # 只告警一次
        lane.beat = time.monotonic()
        lane.ensure()
        self.assertIn("调度线程恢复响应", self.alerts())

    def test_task_exception_does_not_kill_pool(self):
        orig = self.outbox.prepare
        calls = []

        def boom(con, row_id, sender=None, card_sender=None):
            calls.append(row_id)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return orig(con, row_id, sender=sender, card_sender=card_sender)

        self.outbox.prepare = boom
        self.addCleanup(setattr, self.outbox, "prepare", orig)
        self.outbox.upload_image = lambda p: (True, "img", False)
        a = self.image_ticket()
        self.lane(workers=1).ensure()
        self.until(lambda: self.ready(a), msg="出错后没有再试")
        self.assertIn("boom", self.alerts())
        self.assertFalse(self.outbox._claimed_now(self.row(a)["id"]))


class MediaConfigTest(_Base):
    def test_defaults_and_bad_values(self):
        from ticketd import config
        self.assertEqual((config.media_workers(), config.media_video_workers()), (2, 1))
        os.environ.update(TICKETD_MEDIA_WORKERS="4", TICKETD_MEDIA_VIDEO_WORKERS="9")
        self.assertEqual((config.media_workers(), config.media_video_workers()), (4, 4))  # 视频名额不超过总数
        os.environ.update(TICKETD_MEDIA_WORKERS="abc", TICKETD_MEDIA_VIDEO_WORKERS="0")
        self.assertEqual((config.media_workers(), config.media_video_workers()), (2, 1))

    def test_is_video_job(self):
        from ticketd import templates
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "a.png"), PNG)
        self.core.add_report(self.con, tid, "ready", "好了", ["v.mp4", "a.png"])
        self.assertTrue(self.outbox.is_video_job(self.rows()[0]))
        card = json.loads(self.rows()[0]["card_json"])
        for el in card["body"]["elements"]:
            if isinstance(el, dict) and templates.VIDEO_KEY in el:
                el[templates.VIDEO_URL_KEY] = "https://a.feishu.cn/file/x"  # 链接已拿到、只差封面：不占视频名额
        self.con.execute("UPDATE outbox SET card_json=?", (json.dumps(card),))
        self.assertFalse(self.outbox.is_video_job(self.rows()[0]))
        self.assertTrue(self.outbox.needs_media(self.rows()[0], self.con))


class DaemonMediaPoolTest(_Base):
    """真守护进程 + fake lark：A 的视频在云空间上传 8 秒，期间 B（三张图）和 C（稿子）的结果卡照常送达。
    改之前媒体通道只有一个线程，B、C 要排在 A 后面等它传完。"""

    def test_other_tickets_delivered_during_slow_video(self):
        a, wa = self.ticket()
        b, wb = self.ticket()
        c, wc = self.ticket()
        self.write(os.path.join(wa, "v.mp4"), MP4)
        for n in ("1.png", "2.png", "3.png"):
            self.write(os.path.join(wb, n), PNG)
        self.write(os.path.join(wc, "方案.md"), "# 方案\n内容".encode("utf-8"))
        for t in (a, b, c):
            self.con.execute("UPDATE inputs SET status='done' WHERE ticket_id=?", (t,))
            self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (t,))
        self.core.add_report(self.con, a, "ready", "视频好了", ["v.mp4"])
        self.core.add_report(self.con, b, "ready", "图好了", ["1.png", "2.png", "3.png"])
        self.core.add_report(self.con, c, "ready", "稿子好了", ["方案.md"])
        log = self.tmp / "lark.log"
        env = dict(os.environ, FAKE_LARK_DRIVE_SLEEP="8")
        t0 = time.time()
        proc = subprocess.Popen([str(REPO / "bin" / "ticketd")], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def calls():
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

        def status(tid):
            return self.con.execute("SELECT status FROM outbox WHERE ticket_id=? AND report_id IS NOT NULL",
                                    (tid,)).fetchone()["status"]

        sent_at = {}
        try:
            end = time.time() + 25
            while time.time() < end and not all(t in sent_at for t in (a, b, c)):
                for t in (a, b, c):
                    if t not in sent_at and status(t) == "sent":
                        sent_at[t] = time.time() - t0
                if ["drive-upload-finished"] in calls() and "upload_done" not in sent_at:
                    sent_at["upload_done"] = time.time() - t0
                time.sleep(0.05)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=20)
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
        print("\n[媒体任务池实测] 送达时间（秒，从启动守护进程算）：A 视频 %.1f / B 三张图 %.1f / C 稿子 %.1f；"
              "A 的云空间上传在 %.1f 秒结束" % (sent_at.get(a, -1), sent_at.get(b, -1), sent_at.get(c, -1),
                                         sent_at.get("upload_done", -1)))
        self.assertIn("upload_done", sent_at)
        self.assertLess(sent_at[b], sent_at["upload_done"], "B 的图排在 A 的视频后面")
        self.assertLess(sent_at[c], sent_at["upload_done"], "C 的稿子排在 A 的视频后面")
        self.assertIn(a, sent_at)
        self.assertNotIn("未能显示", self.alerts() + "".join(json.dumps(x, ensure_ascii=False) for x in calls()))
        self.assertEqual(sum(1 for x in calls() if x[:3] == ["im", "images", "create"]), 3)
        self.assertEqual(sum(1 for x in calls() if x[:2] == ["drive", "+import"]), 1)
        self.assertEqual(sum(1 for x in calls() if x[:2] == ["drive", "+upload"]), 1)
