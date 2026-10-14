"""A drive launch allocates a placeholder agent row and closes it as "drive predecessor" before
its session takes the row over. Those rows were never agents: status and watch must not list them
(a seat resumed every turn left dozens of them in the pane)."""
from test_hooks_cli import Env


class LaunchPlaceholdersHidden(Env):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        with self.board() as b:
            self.ghost = b.allocate_name("launch:1", "J", "engineer")
            b.close_agent("launch:1", "drive predecessor")
            self.worker = b.allocate_name("w", "J", "engineer")
            b.close_agent("w", "done")

    def test_status_does_not_list_launch_placeholders(self):
        rc, out, err = self.cli("status", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertIn(self.worker, out)
        self.assertNotIn("drive predecessor", out)   # the worker may reuse the placeholder's freed name

    def test_watch_rows_leave_out_launch_placeholders(self):
        with self.board() as b:
            rows, hidden = b.watch_agents("J", None)
            keys = [a.agent_key for a in rows]
            self.assertIn("w", keys)
            self.assertNotIn("launch:1", keys)   # by key: a backend may give "w" the placeholder's freed name
            rows, hidden = b.watch_agents("J", 0)
            self.assertNotIn("launch:1", [a.agent_key for a in rows])
            self.assertEqual(hidden, 1)   # only the real finished worker is "older"


if __name__ == "__main__":
    import unittest
    unittest.main()
