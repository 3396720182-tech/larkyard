from __future__ import annotations

import json
import os
from pathlib import Path

from helpers import FAKES, TicketdTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return [json.loads(x) for x in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if x.strip()]


def normalize_all(raw):
    from ticketd import executors
    n = executors.get("codex").normalizer()
    out = []
    for ev in raw:
        out += n(ev)
    return out


class CodexNormalizeTest(TicketdTestCase):
    """用 codex-cli 0.134.0 的真实输出（V1 实测时录下）校验翻译层。"""

    def test_real_commands_stream(self):
        from ticketd import worker
        evs = normalize_all(load("codex_0134_commands.jsonl"))
        self.assertEqual(evs[0]["type"], "system")
        self.assertEqual(evs[0]["session_id"], "01a1977a-20f4-7ec3-b47c-60bccd11c55b")
        steps = [s for s in (worker.last_step(e) for e in evs) if s]
        self.assertEqual(steps, ["运行 echo", "运行 lark-cli", "运行 ticket"])
        actions = [e for e in evs if worker.is_action(e)]
        self.assertEqual(len(actions), 6)  # 3 条命令 × (开始 + 结束)；被规则拒绝的那条不产生事件
        self.assertEqual(evs[-1]["type"], "result")
        self.assertFalse(evs[-1]["is_error"])
        texts = [c["text"] for e in evs if e["type"] == "assistant"
                 for c in e["message"]["content"] if c["type"] == "text"]
        self.assertTrue(all(t.startswith("[DI-OK]") for t in texts))  # developer_instructions 生效

    def test_real_file_change(self):
        from ticketd import worker
        evs = normalize_all(load("codex_0134_file_change.jsonl"))
        steps = [s for s in (worker.last_step(e) for e in evs) if s]
        self.assertEqual(steps, ["运行 pwd", "写 note.txt"])

    def test_real_turn_failed_is_transient(self):
        from ticketd import worker
        evs = normalize_all(load("codex_0134_turn_failed.jsonl"))
        result = [e for e in evs if e["type"] == "result"][-1]
        self.assertTrue(result["is_error"])
        self.assertTrue(worker.is_transient(1, result, ""))
        lines = [l for e in evs for l in worker.summarize_event(e)]
        self.assertTrue(any(l.startswith("提示：Reconnecting") for l in lines))

    def test_non_transient_failure(self):
        from ticketd import worker
        evs = normalize_all([{"type": "turn.failed", "error": {"message": "invalid model"}}])
        self.assertFalse(worker.is_transient(1, evs[0], ""))

    def test_rate_limit_is_transient(self):
        from ticketd import worker
        evs = normalize_all([{"type": "turn.failed",
                              "error": {"message": "exceeded retry limit, last status: 429 Too Many Requests"}}])
        self.assertTrue(worker.is_transient(1, evs[0], ""))

    def test_odd_shapes_do_not_crash(self):
        evs = normalize_all([{"type": "item.started"}, {"type": "item.completed", "item": "x"},
                             {"type": "item.completed", "item": {"type": "reasoning", "text": "想"}},
                             {"type": "item.started", "item": {"type": "file_change", "changes": None}},
                             {"type": "turn.failed", "error": "boom"}, {"type": "???"}])
        self.assertEqual([e["type"] for e in evs], ["assistant", "result"])


class CodexCommandTest(TicketdTestCase):
    def ticket(self, **kw):
        t = {"id": "T0001", "workdir": str(self.tmp / "w"), "session_id": "uuid-x", "session_started": 0,
             "executor": "codex"}
        t.update(kw)
        return t

    def test_first_turn(self):
        from ticketd import executors
        g = self.tmp / "grant"
        cmd = executors.get("codex").build_command(self.ticket(), [str(g)], "提示\n第二行\"引号\"")
        self.assertEqual(cmd[1:4], ["exec", "-C", str(self.tmp / "w")])
        self.assertIn("--json", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(cmd[-1], "-")  # 任务卡从 stdin 读
        joined = " ".join(cmd)
        self.assertIn('approval_policy="never"', joined)
        self.assertIn('sandbox_mode="workspace-write"', joined)
        self.assertIn("sandbox_workspace_write.network_access=true", joined)
        self.assertIn(json.dumps(str(g.resolve())), joined)
        self.assertEqual(cmd[cmd.index("--disable") + 1], "shell_snapshot")
        self.assertIn('shell_environment_policy.exclude=["*TOKEN*","*KEY*","*SECRET*"]', cmd)
        di = [a for a in cmd if a.startswith("developer_instructions=")][0]
        self.assertEqual(json.loads(di.split("=", 1)[1]), "提示\n第二行\"引号\"")
        self.assertNotIn("-m", cmd)

    def test_git_writable_flag(self):
        from ticketd import perms
        repo, plain = self.tmp / "repo", self.tmp / "plain"
        (repo / ".git").mkdir(parents=True)
        plain.mkdir()
        roots = perms.codex_writable_roots(str(self.tmp / "w"), [str(repo), str(plain)])
        self.assertIn(str(repo.resolve() / ".git"), roots)  # 默认开：和 Claude worker 一样能 commit
        self.assertNotIn(str(plain.resolve() / ".git"), roots)
        os.environ["TICKETD_CODEX_GIT_WRITABLE"] = "0"
        roots = perms.codex_writable_roots(str(self.tmp / "w"), [str(repo), str(plain)])
        self.assertNotIn(str(repo.resolve() / ".git"), roots)

    def test_resume_and_model(self):
        from ticketd import executors
        os.environ["TICKETD_CODEX_MODEL"] = "gpt-x"
        cmd = executors.get("codex").build_command(self.ticket(session_started=1, session_id="th-1"), [], "p")
        self.assertEqual(cmd[1:4], ["exec", "resume", "th-1"])
        self.assertNotIn("-C", cmd)  # resume 不接受 -C
        self.assertNotIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-x")

    def test_pid_marker_and_takeover(self):
        from ticketd import executors
        ex = executors.get("codex")
        self.assertIn(str(self.tmp / "w"), ex.pid_markers(self.ticket()))
        # 首轮中途拿到 thread_id 后重启：命令行里仍只有 -C <工单目录>，必须还能认出来
        self.assertIn(str(self.tmp / "w"), ex.pid_markers(self.ticket(session_started=1, session_id="th-1")))
        self.assertIn("th-1", ex.pid_markers(self.ticket(session_started=1, session_id="th-1")))
        os.environ["TICKETD_CODEX_HOME"] = "/h"
        self.assertIn("CODEX_HOME=/h codex resume th-1", ex.takeover_hint(self.ticket(session_started=1,
                                                                                       session_id="th-1")))

    def test_rules_only_in_dedicated_home(self):
        from ticketd import executors
        executors.get("codex").build_command(self.ticket(), [], "p")  # 没配专用 home：不写规则、不报错
        home = self.tmp / "codex-home"
        os.environ["TICKETD_CODEX_HOME"] = str(home)
        executors.get("codex").build_command(self.ticket(), [], "p")
        self.assertIn('"+messages-send"', (home / "rules" / "ticketd.rules").read_text(encoding="utf-8"))

    def test_env_sets_codex_home(self):
        from ticketd import worker
        os.environ["TICKETD_CODEX_HOME"] = "/h"
        self.assertEqual(worker.worker_env(self.ticket())["CODEX_HOME"], "/h")
        self.assertNotEqual(worker.worker_env(self.ticket(executor="claude")).get("CODEX_HOME"), "/h")

    def test_prompt_addendum(self):
        from ticketd import executors
        self.assertIn("用 Codex 跑时的补充", executors.prompt_text("codex"))
        self.assertNotIn("用 Codex 跑时的补充", executors.prompt_text("claude"))


class CodexSchedulerTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        os.environ["TICKETD_CODEX"] = str(FAKES / "fake_codex.py")
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)

    def tearDown(self):
        self.sched.shutdown()
        super().tearDown()

    def state(self, tid):
        return self.core.get(self.con, tid)["state"]

    def argv_log(self, tid):
        p = os.path.join(self.core.get(self.con, tid)["workdir"], ".fake_argv.jsonl")
        with open(p, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def codex_ticket(self, request="做个测试 [[fake:ready]]", message="om_1"):
        return self.new_ticket(request=request, message=message, executor="codex")

    def test_default_executor_is_claude(self):
        tid = self.new_ticket()
        self.assertEqual(self.core.get(self.con, tid)["executor"], "claude")
        os.environ["TICKETD_EXECUTOR"] = "codex"
        tid2 = self.new_ticket(message="om_2")
        self.assertEqual(self.core.get(self.con, tid2)["executor"], "codex")

    def test_unknown_executor_rejected(self):
        with self.assertRaises(self.core.TicketError):
            self.new_ticket(executor="gemini")

    def test_ready_flow_records_thread_id(self):
        tid = self.codex_ticket()
        placeholder = self.core.get(self.con, tid)["session_id"]
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        t = self.core.get(self.con, tid)
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        self.assertEqual(t["session_started"], 1)
        self.assertNotEqual(t["session_id"], placeholder)
        self.assertTrue(t["session_id"].startswith("01a1"))
        first = self.argv_log(tid)[0]
        self.assertEqual(first["argv"][:2], ["exec", "-C"])
        self.assertIn("# 工单 T0001", first["card"])
        self.assertEqual(t["last_step"], "写 结果.md")
        log = open(os.path.join(t["workdir"], "过程.log"), encoding="utf-8").read()
        self.assertIn("会话就绪 %s" % t["session_id"], log)
        self.assertIn("本轮结束：success", log)
        self.assertIsNone(self.con.execute("SELECT * FROM locks").fetchone())

    def test_followup_resumes_thread(self):
        tid = self.codex_ticket()
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        thread = self.core.get(self.con, tid)["session_id"]
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: len(self.con.execute("SELECT * FROM reports").fetchall()) == 2, self.sched)
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        second = self.argv_log(tid)[1]
        self.assertEqual(second["argv"][:3], ["exec", "resume", thread])
        self.assertIn("用户追问（消息 om_2）", second["card"])
        self.assertNotIn("# 工单", second["card"])
        self.assertEqual(self.core.get(self.con, tid)["session_id"], thread)

    def test_transient_after_thread_resumes(self):
        tid = self.codex_ticket(request="[[fake:transient_once]]")
        self.wait(lambda: self.core.last_report(self.con, tid) is not None, self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["summary"], "重试后完成")
        runs = self.argv_log(tid)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1]["argv"][1], "resume")
        self.assertIn("上一轮因网络或模型接口临时故障中断", runs[1]["card"])

    def test_transient_before_thread_reruns_first_turn(self):
        tid = self.codex_ticket(request="[[fake:prethread_once]]")
        self.wait(lambda: self.core.last_report(self.con, tid) is not None, self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["summary"], "首轮重跑后完成")
        runs = self.argv_log(tid)
        self.assertEqual([r["argv"][1] for r in runs], ["-C", "-C"])
        self.assertIn("# 工单 T0001", runs[1]["card"])

    def test_noreport_fails_with_last_text(self):
        tid = self.codex_ticket(request="[[fake:noreport]]")
        self.wait(lambda: self.state(tid) == "failed", self.sched)
        self.assertIn("我忘了汇报", self.core.last_report(self.con, tid)["summary"])

    def test_persistent_transient_gives_up(self):
        tid = self.codex_ticket(request="[[fake:transient]]")
        self.wait(lambda: self.state(tid) == "failed", self.sched)
        self.assertEqual(len(self.argv_log(tid)), 4)  # 首次 + 3 次重试

    def test_blocked(self):
        tid = self.codex_ticket(request="[[fake:blocked]]")
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        r = self.core.last_report(self.con, tid)
        self.assertEqual((r["status"], r["ask"]), ("blocked", "能授权 /x 吗？"))


class CodexCliTest(TicketdTestCase):
    def run_main(self, *argv):
        import contextlib
        import io
        from ticketd import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_new_with_executor_and_show(self):
        from ticketd import core
        code, _ = self.run_main("new", "--chat", "oc_1", "--message", "om_9", "--title", "X", "--slug", "x",
                                "--request", "做 X", "--ack", "开工", "--executor", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(core.get(self.con, "T0001")["executor"], "codex")
        code, out = self.run_main("show", "T0001")
        self.assertIn("执行器：codex", out)
        self.assertIn("终端接管：（Codex 会话还没建立）", out)
