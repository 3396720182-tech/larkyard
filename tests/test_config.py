from __future__ import annotations

import os

from helpers import TicketdTestCase


class ConfigTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import config, core
        self.config, self.core = config, core
        self.config.home().mkdir(parents=True, exist_ok=True)

    def write_config(self, text):
        p = self.config.config_file()
        p.write_text(text, encoding="utf-8")
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + len(text)))  # 同一秒内多次改写也要让缓存失效

    def test_file_values_and_env_precedence(self):
        os.environ.pop("TICKETD_OWNER")
        os.environ.pop("TICKETD_MAX_WORKERS")
        self.write_config('# 注释\nTICKETD_OWNER="ou_from_file"\nexport TICKETD_MAX_WORKERS=5\n\nnot a line\n')
        self.assertEqual(self.config.owner(), "ou_from_file")
        self.assertEqual(self.config.max_workers(), 5)
        os.environ["TICKETD_OWNER"] = "ou_from_env"
        self.assertEqual(self.config.owner(), "ou_from_env")

    def test_file_change_is_picked_up(self):
        os.environ.pop("TICKETD_MODEL", None)
        self.write_config("TICKETD_MODEL=a\n")
        self.assertEqual(self.config.model(), "a")
        self.write_config("TICKETD_MODEL=bb\n")
        self.assertEqual(self.config.model(), "bb")

    def test_workroot_expands_home(self):
        os.environ["TICKETD_WORKROOT"] = "~/somewhere"
        self.assertEqual(self.config.workroot(), self.config.Path.home() / "somewhere")

    def test_missing_owner_refuses_sender(self):
        os.environ.pop("TICKETD_OWNER")
        with self.assertRaises(self.core.TicketError) as cm:
            self.new_ticket(sender="ou_anyone")
        self.assertIn("TICKETD_OWNER", str(cm.exception))
        self.assertEqual(self.new_ticket(), "T0001")  # 不带 --sender（终端里手动派单）不受影响
