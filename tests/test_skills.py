"""控制台下发的技能（ticketd/skills.py，docs/spec.md §12「下发技能」）：校验、落盘、随心跳同步、失效时删除、
worker 以 --plugin-dir 加载、沙箱里只读、发布者签名。假控制台见 test_console.FakeConsole。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

import signing
from test_console import TOKEN, ConsoleBase, Keys, err

from ticketd import console, executors, perms, skills, update, updsig

D1 = "a" * 64
D2 = "b" * 64
DE = "e" * 64
SKILL_MD = "---\nname: demo\ndescription: 演示\n---\n跑 scripts/run.py\n"


FAKE_SIG = "-----BEGIN SSH SIGNATURE-----\nx\n-----END SSH SIGNATURE-----\n"


def sign_skill(name, files, key, namespace=skills.NAMESPACE):
    """按发布者的做法签一个技能：ssh-keygen -Y sign，命名空间 larkyard-skill。"""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "skill.json"
        f.write_bytes(skills.canonical(name, files))
        subprocess.run([updsig.ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", namespace, str(f)], check=True,
                       capture_output=True)
        return (Path(d) / "skill.json.sig").read_text()


def bundle(digest=D1, password="pw-0123456789", name="demo", key=None, files=None):
    files = files or {"SKILL.md": SKILL_MD, "scripts/run.py": "print(1)\n"}
    sig = sign_skill(name, files, key) if key else FAKE_SIG
    return {"digest": digest, "skills": [{"name": name, "files": dict(files), "secrets": {"password": password},
                                          "sig": sig}]}


class ParseTest(ConsoleBase):
    def bad(self, resp):
        with self.assertRaises(skills.Invalid):
            skills.parse(resp)

    def test_ok(self):
        d, s = skills.parse(bundle())
        self.assertEqual(d, D1)
        self.assertEqual(s[0]["name"], "demo")
        self.assertEqual(skills.parse({"digest": DE, "skills": []}), (DE, []))

    def test_rejects(self):
        def with_files(files):
            b = bundle()
            b["skills"][0]["files"] = files
            return b
        self.bad([])
        self.bad({"digest": "x", "skills": []})
        self.bad({"digest": D1})
        self.bad({"digest": D1, "skills": [bundle()["skills"][0]] * 2})                  # 重名
        for name in ("Demo", "../x", "", "a" * 41, "-x", "demo\n"):
            b = bundle()
            b["skills"][0]["name"] = name
            self.bad(b)
        for rel in ("../x", "/abs", "a/../b", ".hidden", "a//b", "a/./b", "a b", "a/b/c/d/e", ""):
            self.bad(with_files({"SKILL.md": "x", rel: "y"}))
        self.bad(with_files({"run.py": "x"}))                                           # 没有 SKILL.md
        self.bad(with_files({"SKILL.md": "x", "a": "1", "a/b": "2"}))                     # 既是文件又是目录
        self.bad(with_files({"SKILL.md": 1}))
        self.bad(with_files({"SKILL.md": "x" * (skills.MAX_BYTES + 1)}))
        for secrets in ({"Password": "x"}, {"password": "has space"}, {"password": ""}, {"p": "x\n"}, []):
            b = bundle()
            b["skills"][0]["secrets"] = secrets
            self.bad(b)
        b = bundle()
        b["skills"][0]["extra"] = 1
        self.bad(b)
        for sig in (None, "", "not a signature", 5, FAKE_SIG + "x" * skills.SIG_MAX):
            b = bundle()
            b["skills"][0]["sig"] = sig
            self.bad(b)


class InstallTest(ConsoleBase):
    def test_layout_and_modes(self):
        skills.install(*skills.parse(bundle()))
        r = skills.root()
        self.assertEqual((r / "skills" / "demo" / "SKILL.md").read_text(), SKILL_MD)
        self.assertEqual((r / "skills" / "demo" / "scripts" / "run.py").read_text(), "print(1)\n")
        pw = r / "secrets" / "demo" / "password"
        self.assertEqual(pw.read_text(), "pw-0123456789\n")
        self.assertEqual(stat.S_IMODE(pw.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((r / "secrets").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((r / "secrets" / "demo").stat().st_mode), 0o700)
        meta = json.loads((r / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(meta["name"], "larkyard")
        self.assertEqual(skills.installed(), ["demo"])
        self.assertEqual(skills.plugin_dir(), str(r))
        # 技能脚本按「本技能目录往上两级的 secrets/<名字>/」找凭据
        script = r / "skills" / "demo" / "scripts" / "run.py"
        self.assertEqual(script.parents[3] / "secrets" / "demo", pw.parent)

    def test_replace_whole_tree_and_empty(self):
        b = bundle()
        b["skills"][0]["files"]["old.md"] = "旧"
        skills.install(*skills.parse(b))
        skills.install(*skills.parse(bundle(D2, "new-password")))
        r = skills.root()
        self.assertFalse((r / "skills" / "demo" / "old.md").exists())
        self.assertEqual((r / "secrets" / "demo" / "password").read_text(), "new-password\n")
        skills.install(DE, [])
        self.assertEqual(sorted(p.name for p in r.iterdir()), ["state.json"])
        self.assertEqual(skills.state()["digest"], DE)
        self.assertIsNone(skills.plugin_dir())
        self.assertEqual([p.name for p in r.parent.iterdir() if p.name.startswith("skills.")], [])

    def test_failure_keeps_old(self):
        skills.install(*skills.parse(bundle()))
        with mock.patch.object(skills, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                skills.install(*skills.parse(bundle(D2, "x-password")))
        self.assertEqual(skills.state()["digest"], D1)
        self.assertEqual((skills.root() / "secrets" / "demo" / "password").read_text(), "pw-0123456789\n")
        self.assertEqual([p.name for p in skills.root().parent.iterdir() if p.name.startswith("skills.")], [])

    def test_remove(self):
        self.assertFalse(skills.remove())
        skills.install(*skills.parse(bundle()))
        self.assertTrue(skills.remove())
        self.assertFalse(skills.root().exists())
        self.assertIsNone(skills.plugin_dir())

    def test_broken_state_means_nothing_installed(self):
        skills.install(*skills.parse(bundle()))
        (skills.root() / "state.json").write_text("{")
        self.assertIsNone(skills.plugin_dir())


class SyncTest(ConsoleBase):
    """随心跳同步：摘要变了才领；领到空列表删掉；旧版控制台（没有 skills 字段）不动。"""

    def setUp(self):
        super().setUp()
        self.enrolled_state()
        updsig.write_signers(update.main_signers(), updsig.MAIN_PRINCIPAL, signing.pub(self.k.main))
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300, "skills": D1})
        self.fake.routes[("GET", skills.PATH)] = (200, bundle(key=self.k.main))

    def beat(self):
        out = console.send_heartbeat(self.con)
        self.assertTrue(out["ok"], out)
        return out

    def events(self):
        return [(r["code"], r["detail"]) for r in self.con.execute(
            "SELECT code, detail FROM events WHERE kind='console' ORDER BY id")]

    def test_install_then_no_refetch(self):
        self.beat()
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat", skills.PATH])
        get = self.fake.calls[1]
        self.assertEqual((get["method"], get["body"], get["headers"]["Authorization"]), ("GET", None, "Bearer " + TOKEN))
        self.assertEqual(skills.installed(), ["demo"])
        self.assertIn(("skills", "已装上控制台下发的技能：demo"), self.events())
        self.fake.calls.clear()
        self.beat()
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat"])
        self.assertIn("demo", "\n".join(console.status_lines(self.con)))

    def test_revoked_grant_removes(self):
        self.beat()
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300, "skills": DE})
        self.fake.routes[("GET", skills.PATH)] = (200, {"digest": DE, "skills": []})
        self.beat()
        self.assertEqual(skills.installed(), [])
        self.assertFalse((skills.root() / "secrets").exists())
        self.assertIsNone(skills.plugin_dir())
        self.assertIn(("skills", "控制台下发的技能已全部删除（开通已关闭）"), self.events())
        self.fake.calls.clear()
        self.beat()
        self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat"])  # 空列表也记下摘要，不会每次都来领

    def test_old_console_without_field_does_nothing(self):
        skills.install(*skills.parse(bundle()))
        for resp in ({"ok": True, "next_in": 300}, {"ok": True, "next_in": 300, "skills": "nothex"},
                     {"ok": True, "next_in": 300, "skills": 5}):
            self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, resp)
            self.fake.calls.clear()
            self.beat()
            self.assertEqual(self.fake.paths(), ["/api/v1/heartbeat"])
            self.assertEqual(skills.installed(), ["demo"])

    def test_fetch_failure_or_bad_content_keeps_old_and_heartbeat_ok(self):
        skills.install(*skills.parse(bundle()))
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300, "skills": D2})
        for bad in (err("RateLimited", 429), OSError("reset"), (200, {"digest": D2, "skills": [{"name": "../x"}]}),
                    (200, b"x" * (skills.PULL_LIMIT + 1))):
            self.fake.routes[("GET", skills.PATH)] = bad
            out = self.beat()
            self.assertTrue(out["ok"])
            self.assertEqual(skills.state()["digest"], D1)
            self.assertEqual(skills.installed(), ["demo"])
        self.assertIn("skills_failed", [c for c, _ in self.events()])
        self.assertEqual(console.state(self.con), "ok")

    def test_unsigned_wrong_key_tampered_or_release_namespace_rejected(self):
        files = {"SKILL.md": SKILL_MD, "scripts/run.py": "print(1)\n"}
        tampered = bundle(key=self.k.main)
        tampered["skills"][0]["files"]["scripts/run.py"] = "import os; os.system('x')\n"
        renamed = bundle(key=self.k.main)
        renamed["skills"][0]["name"] = "other"
        release_ns = bundle()
        release_ns["skills"][0]["sig"] = sign_skill("demo", files, self.k.main, namespace=updsig.NAMESPACE)
        for b in (bundle(), bundle(key=self.k.evil), bundle(key=self.k.backup), tampered, renamed, release_ns):
            self.fake.routes[("GET", skills.PATH)] = (200, b)
            self.assertTrue(self.beat()["ok"])
            self.assertIsNone(skills.state())
            self.assertFalse(skills.root().exists() and any(skills.root().iterdir()))
        self.assertIn("签名验不过", "\n".join(d for _, d in self.events()))
        self.fake.routes[("GET", skills.PATH)] = (200, bundle(key=self.k.main))
        self.beat()
        self.assertEqual(skills.installed(), ["demo"])

    def test_no_trusted_key_rejects(self):
        update.main_signers().unlink()
        self.beat()
        self.assertIsNone(skills.state())

    def test_big_bundle_within_pull_limit(self):
        files = {"SKILL.md": SKILL_MD, "big.md": "x" * 200000}   # 大于心跳的 64KB 上限、小于领取上限
        self.fake.routes[("GET", skills.PATH)] = (200, bundle(key=self.k.main, files=files))
        self.beat()
        self.assertEqual(len((skills.root() / "skills" / "demo" / "big.md").read_text()), 200000)

    def test_device_revoked_removes(self):
        self.beat()
        self.fake.routes[("POST", "/api/v1/heartbeat")] = err("DeviceRevoked", 401)
        out = console.send_heartbeat(self.con)
        self.assertTrue(out["stopped"])
        self.assertFalse(skills.root().exists())
        self.assertIn("下发的技能和凭据已删除", "\n".join(d for _, d in self.events()))

    def test_stopped_marker_from_update_removes(self):
        self.beat()
        console._write_private(console.stopped_path(), b"DeviceLeft\n")
        console.send_heartbeat(self.con)
        self.assertFalse(skills.root().exists())

    def test_leave_removes(self):
        self.beat()
        lines = console.leave(self.con, out=self.say)
        self.assertIn("控制台下发的技能和凭据已删除", "\n".join(lines))
        self.assertFalse(skills.root().exists())

    def test_not_enrolled_anymore_removes_on_tick(self):
        self.beat()
        console.remove_local()
        console.Heartbeat(clock=time.time).tick()
        self.assertFalse(skills.root().exists())


class WorkerTest(ConsoleBase):
    def ticket(self):
        tid = self.new_ticket()
        return self.con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()

    def test_plugin_dir_only_when_installed(self):
        t = self.ticket()
        cmd = executors.get("claude").build_command(t, [], "prompt")
        self.assertNotIn("--plugin-dir", cmd)
        skills.install(*skills.parse(bundle()))
        cmd = executors.get("claude").build_command(t, [], "prompt")
        i = cmd.index("--plugin-dir")
        self.assertEqual(cmd[i + 1], str(skills.root()))
        skills.install(DE, [])
        self.assertNotIn("--plugin-dir", executors.get("claude").build_command(t, [], "prompt"))

    def test_sandbox_can_read_not_write(self):
        s = perms.build_settings(str(self.tmp / "work" / "T1"), [str(self.tmp)])  # 授权整个上级目录也不能写
        root = str(skills.root().absolute())
        self.assertIn(root, s["sandbox"]["filesystem"]["denyWrite"])
        self.assertNotIn(root, s["sandbox"]["filesystem"]["denyRead"])
        deny = s["permissions"]["deny"]
        for tool in ("Edit", "Write", "NotebookEdit"):
            self.assertIn("%s(/%s/**)" % (tool, root), deny)
        self.assertNotIn("Read(/%s/**)" % root, deny)

    def test_ensure_dirs_creates_empty_skills_dir(self):
        perms.ensure_secret_dirs()
        self.assertTrue(skills.root().is_dir())
        self.assertIsNone(skills.state())
        self.assertIsNone(skills.plugin_dir())
        self.assertFalse(console.enrolled())
        console.Heartbeat(clock=time.time).tick()   # 空目录不算装过，不用删
        self.assertTrue(skills.root().is_dir())
