from __future__ import annotations

import json
from pathlib import Path

from helpers import TicketdTestCase


class PermsTest(TicketdTestCase):
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
        self.assertEqual(sb["network"]["allowedDomains"], ["*"])
        self.assertIn("com.apple.trustd.agent", sb["network"]["allowMachLookup"])

    def test_write_settings_file(self):
        from ticketd import perms
        work = self.tmp / "w"
        work.mkdir()
        p = perms.write_settings("T0001", str(work), [])
        self.assertEqual(p.name, "T0001.json")
        self.assertTrue(json.loads(p.read_text(encoding="utf-8"))["sandbox"]["enabled"])
