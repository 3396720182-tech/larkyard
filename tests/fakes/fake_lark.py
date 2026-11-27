#!/usr/bin/env python3
"""测试用的 lark-cli：记录参数；FAKE_LARK_FAIL_FILE 里的数字表示接下来失败几次。"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
fail_file = os.environ.get("FAKE_LARK_FAIL_FILE")
if fail_file and Path(fail_file).exists():
    n = int(Path(fail_file).read_text() or "0")
    if n > 0:
        Path(fail_file).write_text(str(n - 1))
        retryable = os.environ.get("FAKE_LARK_PERMANENT") != "1"
        if os.environ.get("FAKE_LARK_FAIL_JSON"):  # 指定具体的报错（如 99992361 open_id cross app）
            err = json.loads(os.environ["FAKE_LARK_FAIL_JSON"])
        elif retryable:
            err = {"type": "network", "retryable": True, "message": "boom"}
        else:
            err = {"type": "validation", "code": 230001, "message": "boom"}
        sys.stderr.write("[lark-cli] [WARN] something\n" + json.dumps({"ok": False, "error": err}, indent=2) + "\n")
        sys.exit(4)
log = Path(os.environ["FAKE_LARK_LOG"])
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(args, ensure_ascii=False) + "\n")
count = len(log.read_text(encoding="utf-8").splitlines())
if args[:1] == ["auth"]:
    # 用户身份（T0107）：FAKE_LARK_AUTH_FILE 里写 ok / missing / expired / scope / broken（没有文件按 ok）；
    # 设备码授权成功（--device-code）时写回 ok；FAKE_LARK_LOGIN_FAIL=1 时模拟用户没点、链接过期
    af = os.environ.get("FAKE_LARK_AUTH_FILE")
    state = (Path(af).read_text().strip() if af and Path(af).exists() else "") or "ok"
    granted = os.environ.get("FAKE_LARK_AUTH_GRANTED", "2026-11-20T10:00:00+08:00")
    if args[1:2] == ["status"]:
        if state == "broken":
            sys.stderr.write("keychain not initialized\n")
            sys.exit(1)
        user = {"ok": {"status": "ready", "available": True, "grantedAt": granted},
                "scope": {"status": "ready", "available": True, "grantedAt": granted},
                "missing": {"status": "missing", "message": "User identity: missing (no user logged in)"},
                "expired": {"status": "missing", "message": "User identity: missing (refresh token expired)"}}[state]
        print(json.dumps({"appId": "cli_x", "identities": {"bot": {"status": "ready"}, "user": user}}, indent=2))
    elif args[1:2] == ["check"]:
        if state == "scope":
            print(json.dumps({"ok": False, "granted": [], "missing": ["drive:file:upload"]}))
            sys.exit(1)
        if state in ("missing", "expired"):
            print(json.dumps({"ok": False, "error": "not_logged_in", "missing": []}))
            sys.exit(1)
        print(json.dumps({"ok": True, "granted": args[args.index("--scope") + 1].split(), "missing": []}))
    elif "--no-wait" in args:
        print(json.dumps({"verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=FAKE-CODE",
                          "device_code": "dc_fake_%d" % count, "expires_in": 600, "hint": "x"}))
    elif "--device-code" in args:
        if os.environ.get("FAKE_LARK_LOGIN_PARTIAL") == "1":  # 只同意了部分权限：token 存好了，但退出码 3
            if af:
                Path(af).write_text("scope")
            print(json.dumps({"event": "authorization_complete", "user_open_id": "ou_owner",
                              "warning": {"type": "missing_scope"}}))
            sys.exit(3)
        if os.environ.get("FAKE_LARK_LOGIN_FAIL") == "1":
            sys.stderr.write(json.dumps({"ok": False, "error": {"type": "authentication", "message": "expired_token"}}))
            sys.exit(3)
        if af:
            Path(af).write_text("ok")
        print(json.dumps({"event": "authorization_complete", "user_open_id": "ou_owner"}))
    else:
        print(json.dumps({"ok": True}))
elif args[:2] == ["docs", "+update"]:  # 覆盖已有云文档；FAKE_LARK_UPDATE_FAIL=1 时模拟文档已被删
    if os.environ.get("FAKE_LARK_UPDATE_FAIL") == "1":
        sys.stderr.write(json.dumps({"ok": False, "error": {"type": "api", "code": 1770002, "message": "not found"}}))
        sys.exit(1)
    print(json.dumps({"ok": True, "identity": "user", "data": {}}))
elif args[:2] == ["drive", "+import"]:
    print(json.dumps({"ok": True, "identity": "user",
                      "data": {"type": "docx", "url": "https://example.feishu.cn/docx/fake%d" % count}}, indent=2))
elif args[:2] == ["drive", "+upload"]:  # 云空间上传：只回 file_token（链接要再查 metas）
    if os.environ.get("FAKE_LARK_DRIVE_SLEEP"):  # 模拟大视频慢上传
        import time
        time.sleep(float(os.environ["FAKE_LARK_DRIVE_SLEEP"]))
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(["drive-upload-finished"]) + "\n")
    print(json.dumps({"ok": True, "identity": "user", "data": {"file_token": "boxcn_fake_%d" % count}}, indent=2))
elif args[:3] == ["im", "images", "create"]:  # 图片上传：回 image_key
    print(json.dumps({"ok": True, "identity": "bot", "data": {"image_key": "img_fake_%d" % count}}, indent=2))
elif args[:3] == ["drive", "metas", "batch_query"]:
    tok = json.loads(args[args.index("--data") + 1])["request_docs"][0]["doc_token"]
    print(json.dumps({"ok": True, "identity": "user", "data": {"metas": [
        {"doc_token": tok, "url": "https://example.feishu.cn/file/" + tok}]}}, indent=2))
else:
    print(json.dumps({"ok": True, "identity": "bot", "data": {"message_id": "om_fake_%d" % count}}, indent=2))
