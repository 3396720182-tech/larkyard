#!/usr/bin/env python3
"""端到端自检：在临时目录里起一个 ticketd 调度器，用真实的 claude 和真实的沙箱跑几条关键路径，不碰飞书。

用法：
    /usr/bin/python3 scripts/selftest.py [--model haiku] [--only sandbox,ticket,run,stall] [--keep]

- sandbox：Claude Code 沙箱的语义还和 spec 写的一样吗（默认拒绝本机网络 / unix socket；
  allowLocalBinding、allowUnixSockets 能分别放开）。升级 Claude Code 之后先跑这一项。
- ticket：一张普通的单能跑完一轮、交 ready，成本落库。
- run：worker 用 --run 申请沙箱外执行 → 批准 → ticketd 在沙箱外执行 → 输出交回 worker → worker 交 ready。
- stall：worker 卡在一条不耗 CPU 的命令上 → ticketd 结束这条命令 → worker 收到报错后交汇报。
- 跑过工单类检查后，核对每张单实时记的成本和从原始 stream 重算的是否一致。

用的是真实模型（默认 haiku），每项几美分到十几美分；飞书一侧换成 tests/fakes/fake_lark.py，不发任何消息。
退出码：全部通过 0，有失败 1。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RESULTS = []
CLEANUP = []


def check(name, ok, note=""):
    RESULTS.append((name, ok, note))
    print("%s %-28s %s" % ("✅" if ok else "❌", name, note), flush=True)


# ---------- 沙箱语义 ----------
PROBE = r'''
import os, socket, sys, tempfile
port, sock_path, bind_dir = int(sys.argv[1]), sys.argv[2], sys.argv[3]
def t(name, f):
    try:
        f(); print(name, "OK")
    except Exception as e:
        print(name, "DENIED" if isinstance(e, PermissionError) else "ERR %s" % e)
def tcp():
    socket.create_connection(("127.0.0.1", port), timeout=3).close()
def unix():
    s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(sock_path); s.close()
def bind():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); s.close()
def ubind():
    p = os.path.join(bind_dir, "probe-%d.sock" % os.getpid())
    s = socket.socket(socket.AF_UNIX); s.bind(p); s.close(); os.unlink(p)
for n, f in (("tcp_connect", tcp), ("unix_connect", unix), ("tcp_bind", bind), ("unix_bind", ubind)):
    t(n, f)
'''


def _serve(tmp: Path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    # unix socket 路径有长度上限（macOS 约 104 字节），/var/folders 下的临时目录太长：放到短的 /tmp 下
    sock_dir = Path(tempfile.mkdtemp(prefix="ly-st-", dir="/tmp"))
    CLEANUP.append(sock_dir)
    upath = str(sock_dir / "s.sock")
    usrv = socket.socket(socket.AF_UNIX)
    usrv.bind(upath)
    usrv.listen(5)

    def loop(s):
        while True:
            try:
                c, _ = s.accept()
                c.close()
            except OSError:
                return
    for s in (srv, usrv):
        threading.Thread(target=loop, args=(s,), daemon=True).start()
    return srv.getsockname()[1], upath, str(sock_dir)


def _probe(claude, model, settings: Path, cwd: Path, args) -> dict:
    prompt = ("Run exactly this one Bash command and reply with its raw output only: "
              "/usr/bin/python3 %s %s" % (args[0], " ".join(args[1:])))
    p = subprocess.run([claude, "-p", "--model", model, "--setting-sources", "user", "--permission-mode", "dontAsk",
                        "--settings", str(settings), "--output-format", "text", prompt],
                       cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    out = {}
    for line in p.stdout.splitlines():
        parts = line.strip("` ").split()
        if len(parts) >= 2 and parts[0] in ("tcp_connect", "unix_connect", "tcp_bind", "unix_bind"):
            out[parts[0]] = parts[1]
    return out


def test_sandbox(env, claude, model, tmp: Path):
    from ticketd import perms
    port, upath, udir = _serve(tmp)
    probe = tmp / "probe.py"
    probe.write_text(PROBE)
    wd = tmp / "sbx-wd"
    wd.mkdir()
    real = os.path.realpath(udir)
    cases = [
        ("沙箱：默认全部拒绝", None,
         {"tcp_connect": "DENIED", "unix_connect": "DENIED", "tcp_bind": "DENIED", "unix_bind": "DENIED"}),
        ("沙箱：--local-net", {"local": True},
         {"tcp_connect": "OK", "tcp_bind": "OK", "unix_connect": "DENIED"}),
        ("沙箱：--socket", {"sockets": [udir, real]},
         {"unix_connect": "OK", "unix_bind": "OK", "tcp_connect": "DENIED"}),
    ]
    for name, net, want in cases:
        settings = tmp / ("settings-%d.json" % len(RESULTS))
        settings.write_text(json.dumps(perms.build_settings(str(wd), [], net)))
        got = _probe(claude, model, settings, wd, [str(probe), str(port), upath, udir])
        bad = {k: got.get(k) for k, v in want.items() if got.get(k) != v}
        check(name, not bad, "不符：%s" % bad if bad else "")


# ---------- 工单路径 ----------
class Harness:
    def __init__(self):
        from ticketd import core, db
        from ticketd.scheduler import Scheduler
        self.core, self.db = core, db
        self.con = db.connect()
        self.sched = Scheduler(self.con)
        self.n = 0

    def new(self, request: str) -> str:
        self.n += 1
        return self.core.create_ticket(self.con, chat_id="oc_selftest", message_id="om_selftest_%d" % self.n,
                                       title="自检 %d" % self.n, slug="selftest-%d" % self.n, request_text=request,
                                       ack_text="开工：自检，预期 跑通")

    def pump(self, cond, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self.sched.tick()
            if cond():
                return True
            time.sleep(0.5)
        return False

    def final(self, tid):
        return self.con.execute("SELECT * FROM reports WHERE ticket_id=? AND status<>'progress' ORDER BY id DESC"
                                " LIMIT 1", (tid,)).fetchone()

    def idle(self, tid):
        t = self.core.get(self.con, tid)
        return t["state"] in ("idle", "failed") and tid not in self.sched.running and tid not in self.sched.runs

    def log(self, tid) -> str:
        try:
            return (Path(self.core.get(self.con, tid)["workdir"]) / "过程.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def close(self):
        self.sched.shutdown()
        self.con.close()


def test_ticket(h: Harness):
    tid = h.new("在工单目录里写一个文件 hello.txt，内容是 ok。写完执行 ticket report --status ready，"
                "summary 写「写好了」。这是自检，不用做别的。")
    done = h.pump(lambda: h.final(tid) is not None and h.idle(tid), 300)
    rep = h.final(tid)
    wd = Path(h.core.get(h.con, tid)["workdir"])
    ok = done and rep is not None and rep["status"] == "ready" and (wd / "hello.txt").is_file()
    check("工单：跑完一轮交 ready", ok, "" if ok else "状态 %s，汇报 %s" % (
        h.core.get(h.con, tid)["state"], rep["status"] if rep else "无"))
    cost = h.con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM rounds WHERE ticket_id=?", (tid,)).fetchone()[0]
    check("工单：成本落库", cost > 0, "$%.3f" % cost)


def test_run(h: Harness):
    tid = h.new("这是沙箱外执行通道的自检。在工单目录里写一个脚本 selftest.sh，内容只有一行：echo SELFTEST-RUN-OK。"
                "然后按提示词里「需要在沙箱外做的事」申请执行它：ticket report --status blocked --summary "
                "\"申请执行自检脚本\" --run selftest.sh，然后结束本轮。拿到执行结果后，执行 ticket report --status ready，"
                "summary 里原样写上脚本的输出。")
    if not h.pump(lambda: h.core.pending_run(h.con, tid) is not None and h.idle(tid), 300):
        rep = h.final(tid)
        check("沙箱外执行：worker 申请", False, "没等到申请（汇报：%s）" % (rep["summary"][:80] if rep else "无"))
        return
    check("沙箱外执行：worker 申请", True)
    h.core.approve(h.con, tid)
    ok = h.pump(lambda: (h.final(tid) is not None and h.final(tid)["status"] == "ready" and h.idle(tid)), 300)
    run = h.con.execute("SELECT * FROM runs WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    rep = h.final(tid)
    good = ok and run["status"] == "done" and run["exit_code"] == 0 and "SELFTEST-RUN-OK" in (rep["summary"] or "")
    check("沙箱外执行：执行并交回 worker", good, "" if good else "run=%s/%s，汇报：%s" % (
        run["status"], run["exit_code"], rep["summary"][:80] if rep else "无"))


def test_stall(h: Harness):
    from ticketd import scheduler
    os.environ.update({"TICKETD_STALL_SECONDS": "8", "TICKETD_STALL_INTERRUPT": "20", "TICKETD_STALL_ABORT": "150"})
    scheduler.STALL_SAMPLE = 3.0
    try:
        tid = h.new("这是卡住处置的自检。用 Bash 工具在前台（不要放到后台，timeout 设 600000）运行这一条命令："
                    "/usr/bin/python3 -c 'import time; time.sleep(900)'  它会一直挂着。命令结束后（不管成功还是报错），"
                    "执行 ticket report --status blocked --summary \"命令被中断了\" --ask \"自检，不用回答\"。")
        ok = h.pump(lambda: h.final(tid) is not None and h.idle(tid), 240)
        log = h.log(tid)
        interrupted = "已结束这次命令" in log
        check("卡住：结束卡住的命令", interrupted, "" if interrupted else "过程.log 里没有中断记录")
        rep = h.final(tid)
        good = ok and rep is not None and "已中止" not in rep["summary"]
        check("卡住：worker 收到报错后自己汇报", good, "" if good else "汇报：%s" % (rep["summary"][:80] if rep else "无"))
    finally:
        for k in ("TICKETD_STALL_SECONDS", "TICKETD_STALL_INTERRUPT", "TICKETD_STALL_ABORT"):
            os.environ.pop(k, None)
        scheduler.STALL_SAMPLE = 30.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--only", default="sandbox,ticket,run,stall")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    a = ap.parse_args()
    only = set(a.only.split(","))
    claude = os.environ.get("TICKETD_CLAUDE") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    tmp = Path(tempfile.mkdtemp(prefix="ticketd-selftest-")).resolve()
    os.environ.update({
        "TICKETD_HOME": str(tmp / "home"), "TICKETD_WORKROOT": str(tmp / "work"), "TICKETD_OWNER": "ou_selftest",
        "TICKETD_CLAUDE": claude, "TICKETD_MODEL": a.model, "TICKETD_EXECUTOR": "claude",
        "TICKETD_LARK": str(REPO / "tests" / "fakes" / "fake_lark.py"), "FAKE_LARK_LOG": str(tmp / "lark.log"),
        "TICKETD_DIGEST": "",
    })
    for k in ("TICKETD_SPOOL", "TICKETD_TICKET"):
        os.environ.pop(k, None)
    print("自检目录：%s（模型 %s，claude %s）" % (tmp, a.model, claude), flush=True)
    h = None
    try:
        if "sandbox" in only:
            test_sandbox(os.environ, claude, a.model, tmp)
        if only & {"ticket", "run", "stall"}:
            h = Harness()
            if "ticket" in only:
                test_ticket(h)
            if "run" in only:
                test_run(h)
            if "stall" in only:
                test_stall(h)
            cost = h.con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM rounds").fetchone()[0]
            print("worker 成本合计约 $%.3f" % cost)
            from ticketd import metrics
            checked, bad = metrics.reconcile_costs(h.con)
            check("成本：实时记账和原始记录一致", checked > 0 and not bad,
                  "核对 %d 张单%s" % (checked, "，对不上：%s" % bad if bad else ""))
    finally:
        if h is not None:
            h.close()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        for d in CLEANUP:
            shutil.rmtree(d, ignore_errors=True)
    bad = [r for r in RESULTS if not r[1]]
    print("\n%d 项通过，%d 项失败" % (len(RESULTS) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
