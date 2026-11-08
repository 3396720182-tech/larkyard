"""运营指标：成本落库、紧急事件私信、ticket stats、每周概况。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from helpers import TicketdTestCase


class CostTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)

    def tearDown(self):
        self.sched.shutdown()
        super().tearDown()

    def idle(self, tid):
        self.wait(lambda: self.core.get(self.con, tid)["state"] == "idle" and tid not in self.sched.running, self.sched)

    def rounds(self, tid):
        return [(round(r["cost_usd"], 2), r["outcome"]) for r in
                self.con.execute("SELECT * FROM rounds WHERE ticket_id=? ORDER BY id", (tid,))]

    def test_session_cumulative_cost_becomes_round_cost(self):
        os.environ["FAKE_COST"] = "1.5"
        tid = self.new_ticket()
        self.idle(tid)
        os.environ["FAKE_COST"] = "4.0"  # 同一会话的累计值
        self.core.send_followup(self.con, tid, "om_2", "再改 [[fake:ready]]")
        self.wait(lambda: len(self.rounds(tid)) == 2, self.sched)
        self.idle(tid)
        t = self.core.get(self.con, tid)
        self.assertEqual(self.rounds(tid), [(1.5, "ready"), (2.5, "ready")])
        self.assertAlmostEqual(t["cost_usd"], 4.0)
        self.assertAlmostEqual(t["session_cost"], 4.0)
        # 换新会话：累计值从头算
        self.core.restart(self.con, tid)
        self.assertEqual(self.core.get(self.con, tid)["session_cost"], 0)
        os.environ["FAKE_COST"] = "0.7"
        self.wait(lambda: len(self.rounds(tid)) == 3, self.sched)
        self.idle(tid)
        self.assertEqual(self.rounds(tid)[-1], (0.7, "ready"))
        self.assertAlmostEqual(self.core.get(self.con, tid)["cost_usd"], 4.7)

    def test_failed_round_recorded(self):
        tid = self.new_ticket(request="[[fake:noreport]]")
        self.wait(lambda: len(self.rounds(tid)) == 1, self.sched)
        self.assertEqual(self.rounds(tid)[0][1], "failed")


class StatsTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, metrics
        self.core, self.metrics = core, metrics

    def test_stats_summarizes_week(self):
        now = time.time()
        a = self.new_ticket(title="生图提示词")
        b = self.new_ticket(message="om_2", title="推送")
        ins = "INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome) VALUES(?,?,?,?,?)"
        self.con.execute(ins, (a, now - 100, now - 50, 6.0, "ready"))
        self.con.execute(ins, (a, now - 40, now - 30, 7.5, "blocked"))
        self.con.execute(ins, (b, now - 20, now - 10, 0.5, "failed"))
        self.con.execute(ins, (b, now - 20 * 86400, now - 20 * 86400, 99.0, "ready"))  # 窗口外
        self.con.execute("UPDATE tickets SET session_cost=40 WHERE id=?", (a,))
        self.core.add_report(self.con, a, "blocked", "要推送", run={"path": "push.sh", "script": "git push\n"})
        self.core.add_report(self.con, b, "failed", "模型接口余额不足")
        self.core.send_followup(self.con, a, "om_9", "可以")
        self.metrics.record(self.con, a, "stall_flag")
        text = self.metrics.stats(self.con, 7, now)
        self.assertIn("新开 2 张，跑了 3 轮，worker 成本约 $14", text)
        self.assertIn("最花钱：%s「生图提示词」$14" % a, text)
        self.assertIn("会话已很长", text)
        self.assertIn("申请沙箱外执行 1 次", text)
        self.assertIn("失败 1 次", text)
        self.assertIn("疑似卡住 1", text)
        self.assertIn("你的回复：中位 0 分钟", text)

    def test_backfill_from_stream(self):
        tid = self.new_ticket()
        wd = Path(self.core.get(self.con, tid)["workdir"])
        wd.mkdir(parents=True, exist_ok=True)
        evs = [{"type": "result", "total_cost_usd": c} for c in (1.0, 3.0, 0.5, 2.0)]  # 0.5 = 换了会话
        (wd / ".stream.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        self.assertEqual(self.metrics.backfill_costs(self.con), 1)
        self.assertAlmostEqual(self.core.get(self.con, tid)["cost_usd"], 5.0)
        self.assertEqual(self.metrics.backfill_costs(self.con), 0)  # 可重复执行
