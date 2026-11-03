"""scripts/check_private.py：去敏检查。在临时 git 仓库里造各种「不该公开」的内容，确认拦得住、不回显内容，
也确认本仓库自身是干净的。测试里的假密钥都在运行时拼出来，免得测试文件本身被拦。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

SCRIPT = REPO / "scripts" / "check_private.py"
NOREPLY = "1+dev@users.noreply.github.com"
FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
FAKE_OPEN_ID = "ou" + "_" + "0123456789abcdef" * 2
FAKE_HOME = "/Us" + "ers/realname"
FAKE_MAIL = "someone" + "@" + "mail.example.io"


class CheckPrivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-private-")).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "TICKETD_"))}
        self.env.update({"HOME": str(self.tmp), "TICKETD_HOME": str(self.tmp / "home"),
                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        self.git("init", "-q")
        self.git("config", "user.name", "dev")
        self.git("config", "user.email", NOREPLY)
        self.write("README.md", "hello\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True,
                              check=True).stdout

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def check(self, *args, stdin=None, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, input=stdin)

    def assertBlocked(self, p, *names):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        for n in names:
            self.assertIn(n, p.stdout)

    def test_clean_tree_passes(self):
        self.write("a.md", "fake ids like ou_owner, oc_1, om_1 and /Users/me/larkyard-tickets are fine\n"
                           "author 1+dev@users.noreply.github.com, docs at https://example.feishu.cn/docx/x\n"
                           "Co-Authored-By: Bot <noreply@vendor.io>, alerts from no-reply@vendor.io\n")
        self.git("add", "a.md")  # 整树检查只看已跟踪的文件
        self.assertEqual(self.check().returncode, 0)
        self.write("b.md", "mail " + "jnoreply" + "@vendor.io\n")  # 只认整个本地部分是 noreply 的
        self.git("add", "b.md")
        self.assertBlocked(self.check(), "b.md:1: 邮箱")

    def test_staged_secrets_are_blocked_without_echoing_them(self):
        self.write("leak.py", "TOKEN = %r\nOWNER = %r\nP = '%s/proj'\nMAIL = '%s'\n"
                   % (FAKE_TOKEN, FAKE_OPEN_ID, FAKE_HOME, FAKE_MAIL))
        self.git("add", "leak.py")
        p = self.check("--staged")
        self.assertBlocked(p, "leak.py:1: GitHub 令牌", "leak.py:2: 飞书用户", "leak.py:3: 本机用户目录",
                           "leak.py:4: 邮箱")
        for secret in (FAKE_TOKEN, FAKE_OPEN_ID, "realname", FAKE_MAIL):
            self.assertNotIn(secret, p.stdout + p.stderr)

    def test_staged_only_looks_at_added_lines(self):
        self.write("a.txt", "keep\n")
        self.git("add", "a.txt")
        self.assertEqual(self.check("--staged").returncode, 0)

    def test_staged_checks_commit_identity(self):
        self.git("config", "user.email", FAKE_MAIL)
        self.write("a.txt", "fine\n")
        self.git("add", "a.txt")
        self.assertBlocked(self.check("--staged"), "作者邮箱不是 noreply", "提交者邮箱不是 noreply")

    def test_range_checks_history_message_and_identity(self):
        base = self.git("rev-parse", "HEAD").strip()
        self.write("x.txt", "secret %s\n" % FAKE_TOKEN)
        self.git("add", "x.txt")
        self.git("commit", "-q", "-m", "add x")
        self.git("rm", "-q", "x.txt")  # 后面删掉了，历史里仍然有
        self.git("-c", "user.email=" + FAKE_MAIL, "commit", "-q", "-m", "remove, see " + FAKE_OPEN_ID)
        p = self.check("--range", "%s..HEAD" % base)
        self.assertBlocked(p, "x.txt:1: GitHub 令牌", "作者邮箱不是 noreply", "说明:1: 飞书用户")

    def test_pre_push_reads_refs_from_stdin(self):
        base = self.git("rev-parse", "HEAD").strip()
        self.write("y.txt", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n")
        self.git("add", "y.txt")
        self.git("commit", "-q", "-m", "key")
        head = self.git("rev-parse", "HEAD").strip()
        zero = "0" * 40
        p = self.check("--pre-push", stdin="refs/heads/b %s refs/heads/b %s\n" % (head, base))
        self.assertBlocked(p, "y.txt:1: 私钥")
        # 删除远端分支不用查
        self.assertEqual(self.check("--pre-push", stdin="(delete) %s refs/heads/b %s\n" % (zero, head)).returncode, 0)
        # 新分支：没有远端时查全部提交
        self.assertBlocked(self.check("--pre-push", stdin="refs/heads/b %s refs/heads/b %s\n" % (head, zero)), "私钥")

    def test_personal_patterns_file(self):
        self.write("a.md", "built at Acme Pictures\n")
        self.git("add", "a.md")
        self.assertEqual(self.check().returncode, 0)
        home = self.tmp / "home"
        home.mkdir()
        (home / "private-patterns").write_text("# 公司名\n(?i)acme\\s+pictures\n[bad\n", encoding="utf-8")
        p = self.check()
        self.assertBlocked(p, "a.md:1: 个人规则第 2 行")
        self.assertIn("第 3 行不是合法正则", p.stderr)
        self.assertNotIn("Acme", p.stdout + p.stderr)

    def test_ignore_marker(self):
        self.write("a.md", "see %s/x  <!-- check-private: ignore -->\n" % FAKE_HOME)
        self.git("add", "a.md")
        self.assertEqual(self.check().returncode, 0)

    def test_install_hooks_blocks_commit_and_keeps_foreign_hooks(self):
        p = self.check("--install-hooks")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.write("scripts/check_private.py", SCRIPT.read_text(encoding="utf-8"))
        self.git("add", "scripts")
        self.git("commit", "-q", "-m", "add checker")
        self.write("leak.txt", FAKE_TOKEN + "\n")
        self.git("add", "leak.txt")
        c = subprocess.run(["git", "commit", "-q", "-m", "leak"], cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(c.returncode, 0)
        self.assertIn("GitHub 令牌", c.stdout + c.stderr)
        # 重装是幂等的；已有的别家钩子不覆盖
        self.assertEqual(self.check("--install-hooks").returncode, 0)
        hook = Path(self.git("rev-parse", "--git-path", "hooks").strip())
        if not hook.is_absolute():
            hook = self.repo / hook
        (hook / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertEqual(self.check("--install-hooks").returncode, 1)
        self.assertEqual((hook / "pre-commit").read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

    def test_this_repository_is_clean(self):
        if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO, capture_output=True).returncode:
            self.skipTest("不在 git 克隆里（比如解压的源码包）")
        env = {k: v for k, v in os.environ.items() if k != "TICKETD_HOME"}
        env["TICKETD_HOME"] = str(self.tmp / "no-personal-rules")
        p = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
