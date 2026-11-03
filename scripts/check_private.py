#!/usr/bin/env python3
"""公开仓库的去敏检查：在提交、推送之前拦住不该公开的内容。

查什么（通用规则，写在本文件里）：
  密钥 / 令牌、私钥、真实的飞书 open_id / chat_id / message_id / 应用 id、带真实 token 的飞书文档链接、
  本机用户目录（/Users/<名字>、/home/<名字>，me / you / user / example 这类占位名除外）、
  除 GitHub noreply 和 example.* 以外的邮箱（文件内容、提交信息、作者和提交者都查）。
个人规则（不进仓库）：$TICKETD_HOME/private-patterns（默认 ~/.ticketd/private-patterns），
  一行一个 Python 正则，# 开头是注释。放用户名、公司名、真实 open_id 这类只有你知道的串。

用法：
  check_private.py                   检查当前已跟踪的全部文件
  check_private.py --staged          检查暂存区新增的行和将要使用的作者 / 提交者邮箱（pre-commit）
  check_private.py --range A..B      检查这些提交新增的行、提交信息、作者和提交者邮箱
  check_private.py --pre-push        从标准输入读 git 的 pre-push 参数，检查将要推送的提交
  check_private.py --install-hooks   在本克隆装上 pre-commit 和 pre-push 钩子（只影响本机）
  可选：--patterns FILE 指定个人规则文件。

发现问题时只打印位置和规则名，不回显命中的内容（输出可能被贴进聊天或日志）。
确实是误报的行，在该行写上 check-private: ignore。
退出码：0 没问题；1 有问题；2 用法或 git 出错。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

PLACEHOLDER_USERS = {"me", "you", "user", "username", "example", "runner", "shared", "name", "foo", "alice", "bob"}
# 不算个人信息的邮箱：GitHub noreply、各家服务的 noreply@ / no-reply@（比如提交说明里的 Co-Authored-By）、示例域名
EMAIL_OK = re.compile(r"(@users\.noreply\.github\.com|^no-?reply@[A-Za-z0-9.-]+|@example\.(com|org|net)|@localhost)$",
                      re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
USER_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9_][A-Za-z0-9._-]+)")  # 「/Users/...」这种省略写法不算

RULES: List[Tuple[str, "re.Pattern"]] = [
    ("私钥", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("GitHub 令牌", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("API 密钥", re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
                            r"|AIza[0-9A-Za-z_-]{35})")),
    ("飞书用户/会话/消息 id", re.compile(r"\b(?:ou|oc|om|on)_[0-9a-f]{20,}")),
    ("飞书应用 id", re.compile(r"\bcli_[0-9a-f]{16}\b")),
    ("飞书文档链接", re.compile(r"\.(?:feishu\.cn|larksuite\.com)/(?:docx|docs|wiki|sheets|base|drive/folder|file|minutes)"
                             r"/[A-Za-z0-9]{16,}")),
]
IGNORE_MARK = "check-private: ignore"


def git(*args: str) -> str:
    p = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    if p.returncode:
        print("check_private: git %s 失败：%s" % (" ".join(args[:2]), p.stderr.strip()[:300]), file=sys.stderr)
        raise SystemExit(2)
    return p.stdout


def personal_rules(path: Optional[str]) -> list:
    if path is None:
        home = os.environ.get("TICKETD_HOME") or os.path.join(os.path.expanduser("~"), ".ticketd")
        path = os.path.join(os.path.expanduser(home), "private-patterns")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(("个人规则第 %d 行" % i, re.compile(line)))
        except re.error:
            print("check_private: 个人规则第 %d 行不是合法正则，已跳过" % i, file=sys.stderr)
    return out


def bad_email(addr: str) -> bool:
    return not EMAIL_OK.search(addr.strip())


def scan_line(line: str, rules) -> List[str]:
    if IGNORE_MARK in line:
        return []
    hits = [name for name, rx in rules if rx.search(line)]
    if any(m.group(1).lower() not in PLACEHOLDER_USERS for m in USER_PATH.finditer(line)):
        hits.append("本机用户目录")
    if any(bad_email(m.group(0)) for m in EMAIL.finditer(line)):
        hits.append("邮箱")
    return hits


class Report:
    def __init__(self, rules):
        self.rules = rules
        self.n = 0

    def line(self, where: str, text: str) -> None:
        for name in scan_line(text, self.rules):
            self.add(where, name)

    def add(self, where: str, name: str) -> None:
        self.n += 1
        print("%s: %s" % (where, name))


def scan_tree(rep: Report) -> None:
    for rel in git("ls-files", "-z").split("\0"):
        if not rel:
            continue
        try:
            data = Path(rel).read_bytes()
        except OSError:
            continue  # 已删除未暂存、子模块等
        if b"\0" in data[:8192]:
            continue  # 二进制文件
        for i, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            rep.line("%s:%d" % (rel, i), text)


def scan_diff(rep: Report, diff: str, prefix: str = "") -> None:
    """只看新增的行（-U0 的 diff）。"""
    path, n = "?", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            n = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+"):
            rep.line("%s%s:%d" % (prefix, path, n), raw[1:])
            n += 1


def scan_identity(rep: Report, where: str, author: str, committer: str) -> None:
    for role, addr in (("作者", author), ("提交者", committer)):
        if bad_email(addr):
            rep.add(where, "%s邮箱不是 noreply" % role)


def ident_email(var: str) -> str:
    m = re.search(r"<([^>]*)>", git("var", var))
    return m.group(1) if m else ""


def scan_staged(rep: Report) -> None:
    scan_diff(rep, git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR"))
    scan_identity(rep, "提交身份", ident_email("GIT_AUTHOR_IDENT"), ident_email("GIT_COMMITTER_IDENT"))


def scan_commits(rep: Report, revs: List[str]) -> None:
    for sha in git("rev-list", "--reverse", *revs).split():
        short = sha[:9]
        meta = git("show", "-s", "--format=%ae%x00%ce%x00%B", sha).split("\0", 2)
        scan_identity(rep, "提交 %s" % short, meta[0], meta[1])
        for i, text in enumerate(meta[2].splitlines(), 1):
            rep.line("提交 %s 说明:%d" % (short, i), text)
        scan_diff(rep, git("show", "--format=", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR", sha),
                  prefix="提交 %s " % short)


def pre_push_revs(stdin: Iterable[str]) -> List[List[str]]:
    """pre-push 的标准输入：<本地 ref> <本地 sha> <远端 ref> <远端 sha>。返回每组要检查的 rev-list 参数。"""
    out = []
    for line in stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if set(local_sha) == {"0"}:
            continue  # 删除远端分支
        if set(remote_sha) == {"0"}:
            out.append([local_sha, "--not", "--remotes"])  # 新分支：只查远端还没有的提交
        else:
            out.append(["%s..%s" % (remote_sha, local_sha)])
    return out


HOOK = """#!/bin/sh
# larkyard check-private hook（由 scripts/check_private.py --install-hooks 生成）
PY="${TICKETD_PYTHON:-/usr/bin/python3}"
if [ -z "${TICKETD_PYTHON:-}" ] && [ ! -x "$PY" ]; then PY=python3; fi
exec "$PY" "$(git rev-parse --show-toplevel)/scripts/check_private.py" %s
"""


def install_hooks() -> int:
    hooks = Path(git("rev-parse", "--git-path", "hooks").strip()).resolve()
    hooks.mkdir(parents=True, exist_ok=True)
    for name, arg in (("pre-commit", "--staged"), ("pre-push", "--pre-push")):
        p = hooks / name
        if p.exists() and "larkyard check-private hook" not in p.read_text(encoding="utf-8", errors="replace"):
            print("check_private: 已有别的 %s 钩子，没有覆盖：%s" % (name, p), file=sys.stderr)
            return 1
        p.write_text(HOOK % arg, encoding="utf-8")
        p.chmod(0o755)
        print("installed: %s" % p)
    return 0


def main(argv: List[str]) -> int:
    args = list(argv)
    patterns = None
    if "--patterns" in args:
        i = args.index("--patterns")
        if i + 1 >= len(args):
            print(__doc__, file=sys.stderr)
            return 2
        patterns = args[i + 1]
        del args[i:i + 2]
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    if args == ["--install-hooks"]:
        return install_hooks()
    rep = Report(RULES + personal_rules(patterns))
    if not args:
        scan_tree(rep)
    elif args == ["--staged"]:
        scan_staged(rep)
    elif len(args) == 2 and args[0] == "--range":
        scan_commits(rep, [args[1]])
    elif args == ["--pre-push"]:
        for revs in pre_push_revs(sys.stdin):
            scan_commits(rep, revs)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    if rep.n:
        print("check_private: 发现 %d 处可能不该公开的内容（上面只列位置，不回显内容）。改掉后再提交 / 推送；"
              "确属误报的行写上 %s。" % (rep.n, IGNORE_MARK), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
