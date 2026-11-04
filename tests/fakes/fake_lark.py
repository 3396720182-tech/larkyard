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
        if retryable:
            err = {"type": "network", "retryable": True, "message": "boom"}
        else:
            err = {"type": "validation", "code": 230001, "message": "boom"}
        sys.stderr.write("[lark-cli] [WARN] something\n" + json.dumps({"ok": False, "error": err}, indent=2) + "\n")
        sys.exit(4)
log = Path(os.environ["FAKE_LARK_LOG"])
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(args, ensure_ascii=False) + "\n")
count = len(log.read_text(encoding="utf-8").splitlines())
if args[:2] == ["drive", "+import"]:
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
