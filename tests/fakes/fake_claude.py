#!/usr/bin/env python3
"""测试用的 claude：行为由任务卡里最后一个 [[fake:<mode>]] 标记决定。"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
card = sys.stdin.read()
with Path(".fake_argv.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "card": card}, ensure_ascii=False) + "\n")
# 模拟会话记忆：--resume 时卡片里可能没有标记（如 retry 通知），沿用本工单之前卡片里的最后一个标记
history = [json.loads(x)["card"] for x in Path(".fake_argv.jsonl").read_text(encoding="utf-8").splitlines() if x]
modes = re.findall(r"\[\[fake:([a-z_]+)\]\]", "\n".join(history))
mode = modes[-1] if modes else "ready"
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]


def emit(d):
    print(json.dumps(d, ensure_ascii=False), flush=True)


def report(*args):
    subprocess.run(["ticket", "report"] + list(args), check=True)


emit({"type": "system", "subtype": "init", "session_id": sid})
if mode == "ready":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "做完了"}]}})
    report("--status", "ready", "--summary", "已完成测试任务", "--artifact", "结果.md")
elif mode == "blocked":
    report("--status", "blocked", "--summary", "需要权限", "--ask", "能授权 /x 吗？")
elif mode == "noreport":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "我忘了汇报"}]}})
elif mode == "transient":
    sys.stderr.write("API Error: 529 overloaded\n")
    sys.exit(1)
elif mode == "transient_once":
    marker = Path(".transient_done")
    if not marker.exists():
        marker.write_text("1")
        sys.stderr.write("API Error: 529 overloaded\n")
        sys.exit(1)
    report("--status", "ready", "--summary", "重试后完成")
elif mode == "sleep":
    time.sleep(60)
    report("--status", "ready", "--summary", "睡醒了")
elif mode == "ready_child":
    child = subprocess.Popen(["sleep", "60"])
    Path(".child_pid").write_text(str(child.pid))
    report("--status", "ready", "--summary", "留下了后台进程")
elif mode in ("orphan", "orphan_ready"):
    # 像真实 Bash 工具那样逃出 worker 进程组，但继承 worker 的环境变量
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    Path(".child_pid").write_text(str(child.pid))
    if mode == "orphan":
        time.sleep(60)
    else:
        report("--status", "ready", "--summary", "留下了脱组进程")
elif mode == "progress":
    report("--status", "progress", "--summary", "写核心代码")
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "hi"}]}})
    report("--status", "ready", "--summary", "分阶段完成")
elif mode == "linger":
    # 交了终态汇报、本轮也结束了，但进程还挂着（像真实 claude 在等后台子代理）
    report("--status", "ready", "--summary", "做完了但进程还挂着")
    emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10, "permission_denials": []})
    time.sleep(60)
elif mode == "stall":
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "sleep 600"}}]}})
    time.sleep(60)
elif mode in ("bg_wait", "bg_api_error"):
    # 照 T0005 的真实 stream：后台 Bash 起任务 → 本轮 result（不交汇报）→ 等任务通知 → 唤醒模型接着做
    bg_seconds = float(os.environ.get("FAKE_BG_SECONDS", "3"))
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {
        "command": "genshot shoot", "run_in_background": True}}]}})
    emit({"type": "system", "subtype": "task_started", "task_id": "b1", "description": "后台批量出镜头",
          "task_type": "local_bash"})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "started"}]}})
    if mode == "bg_wait":
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "后台在跑，跑完我再汇报"}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10, "permission_denials": []})
    else:
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text":
              "Failed to authenticate. API Error: 403 insufficient balance"}]}})
        emit({"type": "result", "subtype": "success", "is_error": True, "duration_ms": 10,
              "result": "Failed to authenticate. API Error: 403 insufficient balance", "permission_denials": []})
    time.sleep(bg_seconds)
    emit({"type": "system", "subtype": "task_notification", "task_id": "b1", "status": "completed"})
    emit({"type": "system", "subtype": "init", "session_id": sid})
    if mode == "bg_api_error":
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text":
              "Failed to authenticate. API Error: 403 Insufficient account balance"}]}})
        emit({"type": "result", "subtype": "success", "is_error": True, "duration_ms": 0,
              "result": "Failed to authenticate. API Error: 403 Insufficient account balance"})
        sys.exit(1)
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {
        "command": "genshot sheet", "description": "出联系表"}}]}})
    report("--status", "ready", "--summary", "后台任务跑完了")
elif mode == "poison":
    msg = "API Error: 400 tool_result content[1] is a document, which the tool-result protocol cannot represent"
    emit({"type": "result", "subtype": "success", "is_error": True, "duration_ms": 10, "result": msg})
    sys.exit(1)
elif mode == "balance":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text":
          "Failed to authenticate. API Error: 403 insufficient balance"}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "duration_ms": 10,
          "result": "Failed to authenticate. API Error: 403 insufficient balance"})
    sys.exit(1)
elif mode == "sleep_child":
    child = subprocess.Popen(["sleep", "60"])
    Path(".child_pid").write_text(str(child.pid))
    time.sleep(60)
emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10, "permission_denials": []})
