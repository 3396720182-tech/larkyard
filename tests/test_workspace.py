"""主会话约定跟着版本走（ticketd/workspace.py，T0084）。"""
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase

from ticketd import config, workspace

OLD = "# 飞书桥接工作约定（旧版）\n\n命令：`@LARKYARD@/bin/ticket`\n\n    @LARKYARD@/bin/ticket new --chat <chatId>\n"


class WorkspaceSyncTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        os.environ["TICKETD_WORKSPACE"] = str(self.ws)
        self.md = self.ws / "CLAUDE.md"
        hist = self.tmp / "history"
        hist.write_text("# 说明\n%s  # 旧版\nnot-a-digest\n" % workspace.digest(OLD), encoding="utf-8")
        from ticketd import update
        self.cur = self.tmp / "app dir (x)" / "current"   # 带空格和括号：正则认不出，要靠已知根目录
        for p in (mock.patch.object(workspace, "history_path", return_value=hist),
                  mock.patch.object(update, "is_managed", return_value=True),
                  mock.patch.object(update, "current_link", return_value=self.cur)):
            p.start()
            self.addCleanup(p.stop)
        self.tmpl = workspace.template_path().read_text(encoding="utf-8")
        self.want = workspace.render(self.tmpl, str(self.cur))

    def test_old_template_is_replaced_with_backup(self):
        # 渲染时用的根目录可以是任何路径（克隆、current、经过符号链接的写法）
        old = workspace.render(OLD, "/opt/somewhere/larkyard")
        self.md.write_text(old, encoding="utf-8")
        self.assertEqual(workspace.state()[0], "old")
        os.chmod(str(self.md), 0o640)
        bak = workspace.sync(now=1800000000)
        self.assertTrue(bak)
        self.assertEqual(Path(bak).read_text(encoding="utf-8"), old)
        self.assertEqual(os.stat(bak).st_mode & 0o777, 0o640)   # 备份和新文件都保留原权限
        self.assertEqual(os.stat(str(self.md)).st_mode & 0o777, 0o640)
        self.assertEqual(self.md.read_text(encoding="utf-8"), self.want)
        self.assertEqual(workspace.state()[0], "same")
        self.assertIsNone(workspace.sync())   # 已经是本版：不再动
        # 同一秒里再换一次也不覆盖已有的备份
        self.md.write_text(old, encoding="utf-8")
        bak2 = workspace.sync(now=1800000000)
        self.assertNotEqual(bak, bak2)
        self.assertEqual(Path(bak).read_text(encoding="utf-8"), old)

    def test_existing_original_root_is_kept(self):
        """文件里原来的根目录还在：只换内容，不把主会话的 ticket 换成别的安装。"""
        other = self.tmp / "clone"
        (other / "bin").mkdir(parents=True)
        (other / "bin" / "ticket").write_text("", encoding="utf-8")
        self.md.write_text(workspace.render(OLD, str(other)), encoding="utf-8")
        self.assertTrue(workspace.sync())
        self.assertEqual(self.md.read_text(encoding="utf-8"), workspace.render(self.tmpl, str(other)))
        self.assertEqual(workspace.state()[0], "same")   # 下次启动不再换
        self.assertIsNone(workspace.sync())

    def test_root_with_spaces_is_recognized(self):
        self.md.write_text(workspace.render(OLD, str(self.cur)), encoding="utf-8")
        self.assertEqual(workspace.state()[0], "old")
        self.assertTrue(workspace.sync())
        self.assertEqual(self.md.read_text(encoding="utf-8"), self.want)

    def test_not_managed_never_syncs(self):
        from ticketd import update, cli, host
        self.md.write_text(workspace.render(OLD, "/opt/x"), encoding="utf-8")
        # 显式 mock 平台：CI 也会在真 Windows 上跑（那里克隆也自动同步，见 test_windows_clone_syncs）
        with mock.patch.object(update, "is_managed", return_value=False), \
                mock.patch.object(host, "system", return_value="darwin"):
            self.assertIsNone(workspace.sync())
            self.assertIn("setup-workspace.sh", cli._workspace_drift()[0])
        self.assertEqual(self.md.read_text(encoding="utf-8"), workspace.render(OLD, "/opt/x"))

    def test_windows_root_is_recognized(self):
        """Windows 的安装向导在 Git Bash 里渲染（pwd -W），根目录是 C:/Users/…（T0109）。原来只认 / 开头的，
        认不出 → 当成手改过 → 自检 ❌、也不自动换新版。"""
        for root in ("C:/apps/larkyard", "d:/工作/larkyard", "C:\\apps\\larkyard"):
            with self.subTest(root=root):
                self.md.write_text(workspace.render(OLD, root), encoding="utf-8")
                self.assertEqual(workspace.original_root(self.md.read_text(encoding="utf-8")), root)
                self.assertEqual(workspace.state()[0], "old")
                self.md.write_text(workspace.render(self.tmpl, root), encoding="utf-8")
                with mock.patch.object(workspace, "target_root", side_effect=lambda r: r):   # 这台 Mac 上没有 C:/…
                    self.assertEqual(workspace.state()[0], "same")
        # 「path:/x」里的 h: 不是盘符：照旧认出 /x
        self.assertEqual(workspace.ROOT_RE.findall("see path:/opt/x/bin/ticket"), ["/opt/x"])
        # 路径里有空格：靠本机已知的根目录（Windows 上 Path 是 C:\…，渲染出来是 C:/…）
        from pathlib import PureWindowsPath
        from ticketd import update
        win = PureWindowsPath("C:\\My Apps\\larkyard")
        with mock.patch.object(update, "current_link", return_value=win):
            self.assertEqual(workspace.original_root(workspace.render(OLD, "C:/My Apps/larkyard")),
                             "C:/My Apps/larkyard")

    def test_windows_clone_syncs(self):
        """Windows 的正式安装是一份克隆（还没有受管安装）：Windows 上从克隆运行也照常自动同步；macOS / Linux 的克隆不动。"""
        from ticketd import cli, host, update
        other = self.tmp / "clone"
        (other / "bin").mkdir(parents=True)
        (other / "bin" / "ticket").write_text("", encoding="utf-8")
        self.md.write_text(workspace.render(OLD, str(other)), encoding="utf-8")
        with mock.patch.object(update, "is_managed", return_value=False):
            for name in ("darwin", "linux"):
                with mock.patch.object(host, "system", return_value=name):
                    self.assertFalse(workspace.auto_sync())
                    self.assertIsNone(workspace.sync())
            for name in ("windows", "win32"):   # 原生 Windows 分支映射成 windows；main 上是 sys.platform 原样
                with mock.patch.object(host, "system", return_value=name):
                    self.assertTrue(workspace.auto_sync())
                    self.assertIn("守护进程下次启动时自动换成新版", cli._workspace_drift()[0])
            with mock.patch.object(host, "system", return_value="windows"):
                self.assertTrue(workspace.sync())
        self.assertEqual(self.md.read_text(encoding="utf-8"), workspace.render(self.tmpl, str(other)))

    def test_changed_after_backup_is_not_overwritten(self):
        old = workspace.render(OLD, "/opt/x")
        self.md.write_text(old, encoding="utf-8")
        real = Path.read_bytes
        calls = {"n": 0}

        def racing(path):
            data = real(path)
            if path == self.md:
                calls["n"] += 1
                if calls["n"] == 2:   # sync 写完备份后再读一次：这时有人改了
                    self.md.write_text("别人刚改的\n", encoding="utf-8")
                    return real(path)
            return data
        with mock.patch.object(Path, "read_bytes", racing):
            self.assertIsNone(workspace.sync())
        self.assertEqual(self.md.read_text(encoding="utf-8"), "别人刚改的\n")
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), ["CLAUDE.md"])   # 备份也删了

    def test_hard_link_is_left_alone(self):
        self.md.write_text(workspace.render(OLD, "/opt/x"), encoding="utf-8")
        os.link(str(self.md), str(self.tmp / "other-link"))
        self.assertIsNone(workspace.sync())

    def test_hand_edited_is_left_alone(self):
        edited = workspace.render(OLD, "/opt/x") + "\n我自己加的一条规则\n"
        self.md.write_text(edited, encoding="utf-8")
        self.assertEqual(workspace.state()[0], "edited")
        self.assertIsNone(workspace.sync())
        self.assertEqual(self.md.read_text(encoding="utf-8"), edited)
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), ["CLAUDE.md"])

    def test_two_different_roots_is_not_unrendered(self):
        mixed = OLD.replace("@LARKYARD@", "/a", 1).replace("@LARKYARD@", "/b")
        self.md.write_text(mixed, encoding="utf-8")
        self.assertEqual(workspace.state()[0], "edited")
        self.assertIsNone(workspace.sync())

    def test_missing_and_no_workspace(self):
        self.assertEqual(workspace.state()[0], "missing")
        self.assertIsNone(workspace.sync())
        self.assertFalse(self.md.exists())
        os.environ["TICKETD_WORKSPACE"] = ""
        with mock.patch.dict(os.environ, {"LARK_CHANNEL_HOME": str(self.tmp / "none")}):
            self.assertEqual(workspace.state()[0], "nows")
            self.assertIsNone(workspace.sync())

    def test_symlink_is_left_alone(self):
        real = self.tmp / "real.md"
        real.write_text(workspace.render(OLD, "/opt/x"), encoding="utf-8")
        self.md.symlink_to(real)
        self.assertEqual(workspace.state()[0], "edited")   # doctor 不说「下次自动换」
        self.assertIsNone(workspace.sync())
        self.assertTrue(self.md.is_symlink())

    def test_doctor_line(self):
        from ticketd import cli
        self.md.write_text(workspace.render(OLD, "/opt/x"), encoding="utf-8")
        self.assertTrue(cli._workspace_drift()[0].startswith("⚠️ 主会话约定"))
        self.md.write_text("手写的\n", encoding="utf-8")
        self.assertTrue(cli._workspace_drift()[0].startswith("❌ 主会话约定"))
        self.md.write_text(self.want, encoding="utf-8")
        self.assertEqual(cli._workspace_drift(), ["✅ 主会话约定和本仓库的模板一致"])

    def test_daemon_start_syncs_and_never_raises(self):
        from ticketd import daemon
        self.md.write_text(workspace.render(OLD, "/opt/x"), encoding="utf-8")
        daemon._sync_workspace()
        self.assertEqual(self.md.read_text(encoding="utf-8"), self.want)
        with mock.patch.object(workspace, "sync", side_effect=PermissionError("denied")):
            daemon._sync_workspace()   # 不抛


class TemplateTest(unittest.TestCase):
    def test_generation_always_dispatched(self):
        t = workspace.template_path().read_text(encoding="utf-8")
        self.assertIn("生图、生视频一律派单", t)
        self.assertIn("不要说「没开通」「找管理员加导演组」", t)

    def test_every_committed_template_is_known(self):
        """改模板时要把改之前那一版的摘要追加进 CLAUDE.md.history，不然停在那一版的电脑不会自动换。"""
        try:
            log = subprocess.run(["git", "-C", str(config.REPO), "log", "--format=%H", "--", "workspace/CLAUDE.md.tmpl"],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("没有 git")
        if log.returncode != 0:
            self.skipTest("不是 git 克隆")
        known = workspace.known()
        for sha in log.stdout.split():
            text = subprocess.run(["git", "-C", str(config.REPO), "show", "%s:workspace/CLAUDE.md.tmpl" % sha],
                                  capture_output=True, text=True, timeout=30).stdout
            self.assertIn(workspace.digest(text), known, "模板 %s 不在 CLAUDE.md.history 里" % sha[:7])


if __name__ == "__main__":
    unittest.main()
