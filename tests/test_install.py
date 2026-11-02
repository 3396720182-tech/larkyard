"""scripts/install.sh 和 uninstall.sh：用假的 uname / launchctl / systemctl 等走两个平台的分支，
不碰真实的 launchd 和 systemd，所以在 macOS 和 Linux 上都能跑两套。"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

SH = os.environ.get("TICKETD_TEST_SH", "/bin/sh")  # 想按 Ubuntu 的 /bin/sh 测时设成 dash

FAKE = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_CALLS"
case "$(basename "$0") $*" in
  "uname -s") echo "$FAKE_OS";;
  "uname -r") echo "$FAKE_KERNEL";;
  "uname "*) echo "$FAKE_OS";;
  "launchctl print"*) exit 1;;
  "systemctl --user show-environment") printf '%s' "${FAKE_SYSTEMD_ENV:-}" | tr ';' '\n'; exit "${FAKE_SYSTEMCTL_FAIL:-0}";;
  "loginctl show-user"*) echo "${FAKE_LINGER:-no}";;
esac
exit 0
'''


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-install-")).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        for name in ("uname", "launchctl", "plutil", "systemctl", "loginctl", "node", "lark-cli", "claude",
                     "bwrap", "socat"):
            p = self.fakebin / name
            p.write_text(FAKE, encoding="utf-8")
            p.chmod(0o755)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.calls = self.tmp / "calls.log"
        self.env = {
            "PATH": "%s:/usr/bin:/bin" % self.fakebin,
            "HOME": str(self.home),
            "FAKE_CALLS": str(self.calls),
            "FAKE_KERNEL": "6.8.0-generic",
            "TICKETD_PYTHON": sys.executable,
            "TICKETD_WORKROOT": str(self.tmp / "tickets"),
        }

    def run_script(self, name, *args, os_name="Linux", **env):
        e = dict(self.env, FAKE_OS=os_name)
        e.update(env)
        return subprocess.run([SH, str(REPO / "scripts" / name)] + list(args), capture_output=True, text=True,
                              env=e, timeout=60)

    def calls_of(self, tool):
        if not self.calls.exists():
            return []
        return [line.split(" ", 1)[1] if " " in line else "" for line in self.calls.read_text().splitlines()
                if line.split(" ", 1)[0] == tool]

    # ---------- --print ----------
    def test_print_darwin_plist(self):
        del self.env["TICKETD_PYTHON"]
        p = self.run_script("install.sh", "--print", os_name="Darwin")
        self.assertEqual(p.returncode, 0, p.stderr)
        plist = plistlib.loads(p.stdout.encode("utf-8"))
        self.assertEqual(plist["Label"], "dev.larkyard.ticketd")
        self.assertEqual(plist["ProgramArguments"], [str(REPO / "bin" / "ticketd")])
        self.assertTrue(plist["KeepAlive"])
        env = plist["EnvironmentVariables"]
        self.assertEqual(env["PATH"], "%s/bin:%s:%s:%s:/usr/bin:/bin:/usr/sbin:/sbin" % (
            REPO, self.fakebin, self.fakebin, self.fakebin))
        self.assertEqual(env["TICKETD_HOME"], str(self.home / ".ticketd"))
        self.assertEqual(env["LANG"], "zh_CN.UTF-8")
        self.assertNotIn("TICKETD_PYTHON", env)  # 没指定解释器时 macOS 的 plist 不变
        self.assertNotIn("@", p.stdout.replace("<?xml", ""))
        self.assertEqual(self.calls_of("systemctl"), [])

    def test_print_darwin_plist_with_explicit_python(self):
        p = self.run_script("install.sh", "--print", os_name="Darwin", TICKETD_PYTHON=sys.executable)
        self.assertEqual(p.returncode, 0, p.stderr)
        env = plistlib.loads(p.stdout.encode("utf-8"))["EnvironmentVariables"]
        self.assertEqual(env["TICKETD_PYTHON"], sys.executable)  # 否则 launchd 下的 bin/ticketd 会退回 /usr/bin/python3
        self.assertEqual(env["TICKETD_HOME"], str(self.home / ".ticketd"))

    def unit(self, text):
        env, keys = {}, {}
        for line in text.splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if k == "Environment":
                ek, ev = v.strip('"').split("=", 1)
                env[ek] = ev
            else:
                keys[k] = v
        return keys, env

    def test_print_linux_unit(self):
        p = self.run_script("install.sh", "--print", TICKETD_LABEL="my.ticketd")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("@", p.stdout)
        keys, env = self.unit(p.stdout)
        self.assertEqual(keys["ExecStart"], '"%s/bin/ticketd"' % REPO)
        self.assertEqual(keys["WorkingDirectory"], str(self.home / ".ticketd"))
        self.assertEqual(keys["Restart"], "always")
        self.assertEqual(keys["KillMode"], "mixed")
        self.assertEqual(keys["WantedBy"], "default.target")
        self.assertEqual(keys["StandardError"],
                         "append:%s/.ticketd/logs/daemon.err.log" % self.home)
        self.assertIn("my.ticketd", keys["Description"])
        self.assertTrue(env["PATH"].startswith("%s/bin:" % REPO))
        self.assertTrue(env["PATH"].endswith(":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"))
        self.assertEqual(env["LANG"], "C.UTF-8")  # zh_CN 在很多 Linux 上没生成，会让 Python 退回 ASCII
        self.assertEqual(env["TICKETD_PYTHON"], sys.executable)
        self.assertEqual(env["LARK_CHANNEL"], "1")
        self.assertEqual(env["LARKSUITE_CLI_CONFIG_DIR"], "%s/.lark-channel/profiles/claude/lark-cli" % self.home)

    def test_unsupported_os(self):
        p = self.run_script("install.sh", "--print", os_name="FreeBSD")
        self.assertEqual(p.returncode, 1)
        self.assertIn("不支持 FreeBSD", p.stderr)

    def test_linux_rejects_unit_breaking_paths(self):
        for bad in ("with space", "50%", "a$b"):
            home = self.tmp / bad
            home.mkdir()
            p = self.run_script("install.sh", "--print", HOME=str(home))
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn("不能有空白", p.stderr)

    def test_darwin_still_allows_spaces(self):
        home = self.tmp / "with space"
        home.mkdir()
        p = self.run_script("install.sh", "--print", os_name="Darwin", HOME=str(home))
        self.assertEqual(p.returncode, 0, p.stderr)

    # ---------- 真安装（假 systemctl / launchctl） ----------
    def test_linux_install_and_uninstall(self):
        p = self.run_script("install.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        unit = self.home / ".config" / "systemd" / "user" / "dev.larkyard.ticketd.service"
        self.assertTrue(unit.is_file())
        self.assertTrue((self.home / ".ticketd" / "config.env").is_file())
        self.assertTrue((self.home / ".ticketd" / "logs").is_dir())
        self.assertTrue((self.tmp / "tickets").is_dir())
        self.assertEqual(self.calls_of("systemctl"), [
            "--user show-environment", "--user show-environment", "--user daemon-reload", "--user enable dev.larkyard.ticketd.service",
            "--user restart dev.larkyard.ticketd.service"])
        self.assertIn("TICKETD_OWNER", p.stderr)
        self.assertIn("enable-linger", p.stderr)  # 假 loginctl 说没开 linger
        self.assertNotIn("没有 bwrap", p.stderr)
        self.assertEqual(self.calls_of("launchctl"), [])

        p = self.run_script("uninstall.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(unit.exists())
        self.assertIn("--user disable --now dev.larkyard.ticketd.service", self.calls_of("systemctl"))
        self.assertTrue((self.home / ".ticketd" / "config.env").is_file())  # 数据保留

    def test_linux_unit_goes_where_the_user_manager_looks(self):
        # 用 systemd 用户实例自己的 XDG_CONFIG_HOME，不用当前 shell 的
        p = self.run_script("install.sh", XDG_CONFIG_HOME=str(self.tmp / "shell-cfg"), FAKE_LINGER="yes",
                            FAKE_SYSTEMD_ENV="HOME=%s;XDG_CONFIG_HOME=%s" % (self.home, self.tmp / "cfg"))
        self.assertEqual(p.returncode, 0, p.stderr)
        unit = self.tmp / "cfg" / "systemd" / "user" / "dev.larkyard.ticketd.service"
        self.assertTrue(unit.is_file())
        self.assertFalse((self.tmp / "shell-cfg").exists())
        self.assertNotIn("enable-linger", p.stderr)
        p = self.run_script("uninstall.sh", FAKE_SYSTEMD_ENV="XDG_CONFIG_HOME=%s" % (self.tmp / "cfg"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(unit.exists())

    def test_linux_without_systemd_user_instance(self):
        p = self.run_script("install.sh", FAKE_SYSTEMCTL_FAIL="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("systemd=true", p.stderr)  # 告诉 WSL2 用户怎么开 systemd
        self.assertFalse((self.home / ".ticketd").exists())  # 失败时什么都不写
        self.assertFalse((self.home / ".config").exists())

    def test_linux_warns_missing_sandbox_tools(self):
        (self.fakebin / "bwrap").unlink()
        (self.fakebin / "socat").unlink()
        p = self.run_script("install.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("没有 bwrap", p.stderr)
        self.assertIn("没有 socat", p.stderr)

    def test_wsl1_is_rejected_before_anything_else(self):
        p = self.run_script("install.sh", FAKE_KERNEL="4.4.0-19041-Microsoft")
        self.assertEqual(p.returncode, 1)
        self.assertIn("WSL1", p.stderr)
        self.assertNotIn("systemd=true", p.stderr)  # 不要叫 WSL1 用户去开 systemd
        self.assertFalse((self.home / ".ticketd").exists())

    def test_wsl2_kernel_is_not_flagged(self):
        p = self.run_script("install.sh", FAKE_KERNEL="5.15.153.1-microsoft-standard-WSL2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("WSL1", p.stderr)

    def test_old_python_is_rejected(self):
        old = self.fakebin / "python-old"
        old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # 版本检查失败
        old.chmod(0o755)
        p = self.run_script("install.sh", TICKETD_PYTHON=str(old))
        self.assertEqual(p.returncode, 1)
        self.assertIn("Python 3.9+", p.stderr)

    def test_darwin_install_and_uninstall(self):
        p = self.run_script("install.sh", os_name="Darwin")
        self.assertEqual(p.returncode, 0, p.stderr)
        plist = self.home / "Library" / "LaunchAgents" / "dev.larkyard.ticketd.plist"
        self.assertTrue(plist.is_file())
        uid = str(os.getuid())
        self.assertEqual(self.calls_of("plutil"), ["-lint %s" % plist])
        launch = self.calls_of("launchctl")
        self.assertEqual(launch[0], "bootout gui/%s/dev.larkyard.ticketd" % uid)
        self.assertEqual(launch[-1], "bootstrap gui/%s %s" % (uid, plist))
        self.assertEqual(self.calls_of("systemctl"), [])
        p = self.run_script("uninstall.sh", os_name="Darwin")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(plist.exists())


if __name__ == "__main__":
    unittest.main()
