#!/usr/bin/env python3
"""测试用的 codex：输出 codex exec --json 的事件（形状取自 codex-cli 0.134.0 实测），
行为由任务卡里最后一个 [[fake:<mode>]] 标记决定。"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

argv = sys.argv[1:]
card = sys.stdin.read()
with Path(".fake_argv.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "card": card}, ensure_ascii=False) + "\n")
history = [json.loads(x)["card"] for x in Path(".fake_argv.jsonl").read_text(encoding="utf-8").splitlines() if x]
modes = re.findall(r"\[\[fake:([a-z_]+)\]\]", "\n".join(history))
mode = modes[-1] if modes else "ready"
if "resume" in argv:
    tid = argv[argv.index("resume") + 1]
    store = Path(".fake_codex_threads")
    if tid not in (store.read_text().split() if store.exists() else []):
        sys.stderr.write("Error: no rollout found for thread id %s\n" % tid)
        sys.exit(1)
else:
    tid = "01a1" + uuid.uuid4().hex[4:]


def emit(d):
    print(json.dumps(d, ensure_ascii=False), flush=True)


def item(kind, i, done, **kw):
    emit({"type": "item.completed" if done else "item.started", "item": dict(id="item_%d" % i, type=kind, **kw)})


def report(*args):
    subprocess.run(["ticket", "report"] + list(args), check=True)


if mode == "prethread_once" and not Path(".prethread_done").exists():  # 连 thread.started 都没发出就失败
    Path(".prethread_done").write_text("1")
    sys.stderr.write("Error: stream disconnected before completion: error sending request for url\n")
    sys.exit(1)
emit({"type": "thread.started", "thread_id": tid})
with Path(".fake_codex_threads").open("a") as f:
    f.write(tid + "\n")
emit({"type": "turn.started"})


def fail_turn():
    msg = "stream disconnected before completion: error sending request for url (https://gw/v1/responses)"
    emit({"type": "error", "message": "Reconnecting... 1/5 (%s)" % msg})
    emit({"type": "turn.failed", "error": {"message": msg}})
    sys.exit(1)


if mode == "ready":
    item("command_execution", 1, False, command="/bin/zsh -c 'ls'", aggregated_output="", exit_code=None,
         status="in_progress")
    item("command_execution", 1, True, command="/bin/zsh -c 'ls'", aggregated_output="a\n", exit_code=0,
         status="completed")
    item("file_change", 2, False, changes=[{"path": str(Path("结果.md").resolve()), "kind": "add"}],
         status="in_progress")
    item("file_change", 2, True, changes=[{"path": str(Path("结果.md").resolve()), "kind": "add"}],
         status="completed")
    item("agent_message", 3, True, text="做完了")
    report("--status", "ready", "--summary", "已完成测试任务", "--artifact", "结果.md")
elif mode == "blocked":
    report("--status", "blocked", "--summary", "需要权限", "--ask", "能授权 /x 吗？")
elif mode == "noreport":
    item("agent_message", 1, True, text="我忘了汇报")
elif mode == "transient":
    fail_turn()
elif mode == "transient_once":
    marker = Path(".transient_done")
    if not marker.exists():
        marker.write_text("1")
        fail_turn()
    report("--status", "ready", "--summary", "重试后完成")
elif mode == "prethread_once":
    report("--status", "ready", "--summary", "首轮重跑后完成")
elif mode == "sleep":
    time.sleep(60)
    report("--status", "ready", "--summary", "睡醒了")
emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
