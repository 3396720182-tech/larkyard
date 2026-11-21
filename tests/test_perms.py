from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase


def on(system):
    from ticketd import host
    return mock.patch.object(host, "system", lambda: system)


class PermsTest(TicketdTestCase):
    @on("darwin")
    def test_settings_shape(self):
        from ticketd import config, perms
        work = self.tmp / "work" / "T0001-x"
        work.mkdir(parents=True)
        proj = self.tmp / "proj"
        proj.mkdir()
        s = perms.build_settings(str(work), [str(proj)])
        allow = s["permissions"]["allow"]
        self.assertIn("Edit(/%s/**)" % work, allow)
        self.assertIn("Write(/%s/**)" % proj, allow)
        self.assertTrue(allow[allow.index("Edit(/%s/**)" % work)].startswith("Edit(//"))
        self.assertIn("Bash(lark-cli im +messages-send:*)", s["permissions"]["deny"])
        sb = s["sandbox"]
        self.assertIn("Bash(lark-cli im messages forward:*)", s["permissions"]["deny"])
        self.assertTrue(sb["enabled"])
        self.assertFalse(sb["allowUnsandboxedCommands"])
        w = sb["filesystem"]["allowWrite"]
        self.assertIn(str(work), w)
        self.assertIn(str(proj), w)
        self.assertNotIn(str(config.db_dir().resolve()), w)
        self.assertIn(str(Path.home() / "Library" / "Application Support" / "lark-cli"), w)
        self.assertIn("/private/tmp", w)
        self.assertEqual(sb["network"]["allowedDomains"], ["*"])
        self.assertIn("com.apple.trustd.agent", sb["network"]["allowMachLookup"])
        self.assertTrue(sb["failIfUnavailable"])

    @on("linux")
    def test_settings_linux(self):
        from ticketd import perms
        work = self.tmp / "work" / "T0001-x"
        work.mkdir(parents=True)
        os.environ["HOME"] = str(self.tmp / "h")
        (self.tmp / "h" / ".local" / "share" / "lark-cli").mkdir(parents=True)
        os.environ.pop("LARKSUITE_CLI_DATA_DIR", None)
        s = perms.build_settings(str(work), [])
        sb = s["sandbox"]
        w = sb["filesystem"]["allowWrite"]
        self.assertIn(str(work), w)
        self.assertIn(str(Path.home() / ".local" / "share" / "lark-cli"), w)
        self.assertIn("/tmp", w)
        self.assertNotIn("/private/tmp", w)
        self.assertFalse(any("Library" in p for p in w))
        self.assertEqual(sb["network"], {"allowedDomains": ["*"]})  # mach 服务是 macOS 才有的
        self.assertTrue(sb["enabled"] and sb["failIfUnavailable"])
        self.assertFalse(sb["allowUnsandboxedCommands"])
        self.assertIn("Edit(/%s/**)" % work, s["permissions"]["allow"])

    @on("linux")
    def test_lark_cli_data_dir_linux(self):
        from ticketd import host
        os.environ["LARKSUITE_CLI_DATA_DIR"] = "/srv/lark"
        self.assertEqual(host.lark_cli_data_dir(), "/srv/lark/lark-cli")
        os.environ["LARKSUITE_CLI_DATA_DIR"] = "relative/dir"  # lark-cli 不认相对路径，退回默认
        self.assertEqual(host.lark_cli_data_dir(), str(Path.home() / ".local" / "share" / "lark-cli"))

    @on("darwin")
    def test_lark_cli_data_dir_darwin_ignores_linux_override(self):
        from ticketd import host
        os.environ["LARKSUITE_CLI_DATA_DIR"] = "/srv/lark"
        self.assertEqual(host.lark_cli_data_dir(), str(Path.home() / "Library" / "Application Support" / "lark-cli"))

    @on("linux")
    def test_linux_skips_missing_extra_paths(self):
        # bubblewrap 绑定不存在的路径会失败：附带路径不存在就不写，工单目录和授权目录照写
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        os.environ["HOME"] = str(self.tmp / "nohome")
        os.environ["TMPDIR"] = str(self.tmp / "no-tmp")
        os.environ["LARKSUITE_CLI_CONFIG_DIR"] = str(self.tmp / "no-lark-cfg")
        os.environ.pop("LARKSUITE_CLI_DATA_DIR", None)
        missing_grant = str(self.tmp / "grant-not-yet")
        w = perms.build_settings(str(work), [missing_grant])["sandbox"]["filesystem"]["allowWrite"]
        self.assertEqual(w, [str(work), missing_grant, "/tmp"])
        self.assertEqual(perms.codex_writable_roots(str(work), [missing_grant]), [str(work), missing_grant])

    @on("darwin")
    def test_darwin_keeps_missing_paths(self):
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        os.environ["TMPDIR"] = str(self.tmp / "no-tmp")
        w = perms.build_settings(str(work), [])["sandbox"]["filesystem"]["allowWrite"]
        self.assertIn(str(self.tmp / "no-tmp"), w)

    def test_codex_roots_follow_platform(self):
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        os.environ["HOME"] = str(self.tmp / "h2")
        (self.tmp / "h2" / ".local" / "share" / "lark-cli").mkdir(parents=True)
        os.environ.pop("LARKSUITE_CLI_DATA_DIR", None)
        with on("linux"):
            self.assertIn(str(Path.home() / ".local" / "share" / "lark-cli"),
                          perms.codex_writable_roots(str(work), []))
        with on("darwin"):
            self.assertIn(str(Path.home() / "Library" / "Application Support" / "lark-cli"),
                          perms.codex_writable_roots(str(work), []))

    def test_write_settings_file(self):
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        p = perms.write_settings("T0001", str(work), [])
        self.assertEqual(p.name, "T0001.json")
        self.assertTrue(json.loads(p.read_text(encoding="utf-8"))["sandbox"]["enabled"])


class SecretDirTest(TicketdTestCase):
    """spec §12「凭据不进 worker」：控制台目录（设备令牌、以后的网关令牌）worker 读不到、写不了。"""

    def console_dir(self):
        from ticketd import console
        return str(console.console_dir())

    def check(self, s, d):
        fs = s["sandbox"]["filesystem"]
        self.assertIn(d, fs["denyRead"])  # Bash 和它起的子进程
        self.assertIn(d, fs["denyWrite"])
        deny = s["permissions"]["deny"]
        for tool in ("Read", "Edit", "Write", "NotebookEdit"):  # 内置文件工具（Grep / Glob 跟 Read 规则走）
            self.assertIn("%s(/%s/**)" % (tool, d), deny)
        self.assertIn("Bash(lark-cli im +messages-send:*)", deny)  # 原有护栏还在

    def test_denied_on_both_platforms(self):
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        for system in ("darwin", "linux"):
            with on(system):
                self.check(perms.build_settings(str(work), []), self.console_dir())

    def test_denied_even_when_home_is_granted(self):
        # 授权目录很宽（整个 TICKETD_HOME 的上级）也不放开控制台目录
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        s = perms.build_settings(str(work), [str(self.tmp)])
        self.assertIn(str(self.tmp), s["sandbox"]["filesystem"]["allowWrite"])
        self.check(s, self.console_dir())

    def test_symlinked_home_lists_real_path_too(self):
        from ticketd import perms
        real = self.tmp / "real-home"
        real.mkdir()
        link = self.tmp / "link-home"
        link.symlink_to(real)
        os.environ["TICKETD_HOME"] = str(link)
        s = perms.build_settings(str(self.tmp), [])
        self.check(s, str(link / "console"))
        self.check(s, str(real / "console"))

    def test_write_settings_creates_private_console_dir(self):
        # Linux 的 bubblewrap 只能遮住已存在的路径：每轮写设置时先建好（空目录不算入网）
        from ticketd import console, perms
        work = self.tmp / "w"
        work.mkdir()
        self.assertFalse(console.console_dir().exists())
        perms.write_settings("T0001", str(work), [])
        d = console.console_dir()
        self.assertTrue(d.is_dir())
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        self.assertFalse(console.present())
        self.assertFalse(console.enrolled())


class NetGrantSettingsTest(TicketdTestCase):
    """issue #20：字段名和语义在 Claude Code 2.1.316 上实测过（默认四项全 EPERM；
    allowLocalBinding 放开连本机端口和本机 bind；allowUnixSockets 放开路径下 socket 的连接和 bind）。"""

    def test_default_has_no_local_network(self):
        from ticketd import perms
        net = perms.build_settings("/w", [])["sandbox"]["network"]
        self.assertNotIn("allowLocalBinding", net)
        self.assertNotIn("allowUnixSockets", net)

    def test_grants_map_to_sandbox_fields(self):
        from ticketd import perms
        net = perms.build_settings("/w", [], {"local": True, "sockets": ["/var/run/docker.sock"]})["sandbox"]["network"]
        self.assertIs(net["allowLocalBinding"], True)
        self.assertEqual(net["allowUnixSockets"], ["/var/run/docker.sock"])
        self.assertEqual(net["allowedDomains"], ["*"])
