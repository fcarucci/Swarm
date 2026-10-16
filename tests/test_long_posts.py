"""An addressed post past the message cap arrives whole (head plus a file); a broadcast stays capped."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from test_routing import RoutingEnv
from swarm import paths


class LongPosts(RoutingEnv):
    def setUp(self):
        super().setUp()
        self.share = Path(tempfile.mkdtemp(dir=Path.home()))   # never /tmp
        self.addCleanup(lambda: __import__("shutil").rmtree(self.share, ignore_errors=True))
        patch = mock.patch.object(paths, "share_dir", return_value=self.share)
        patch.start()
        self.addCleanup(patch.stop)
        self.activate("J")
        self.spawn("a1", "[swarm job: J]\n[swarm role: engineer]\nBuild.")
        self.spawn("a2", "[swarm job: J]\n[swarm role: qa]\nTest.")
        self.a1, self.a2 = self.member("a1").name, self.member("a2").name

    def last(self):
        with self.board() as b:
            cap = b.message_cap()
            return b.recent_messages(1, "J")[0], cap

    def test_addressed_long_post_is_delivered_whole_through_a_file(self):
        order = "Order1 step. " * 120   # ~1.5k chars
        rc, out, err = self.cli("post", "--job", "J", "--as", self.a1, "--to", self.a2, order)
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn("truncated", out)
        m, cap = self.last()
        self.assertLessEqual(len(m.message), cap)
        self.assertEqual(m.to_agent, self.a2)
        self.assertIn("full text:", m.message)
        path = Path(m.message.split("full text: ")[1])
        self.assertEqual(path.parent.parent.parent, self.share)
        self.assertEqual(" ".join(path.read_text().split()), order.strip())

    def test_short_addressed_and_long_broadcast_unchanged(self):
        rc, out, _ = self.cli("post", "--job", "J", "--as", self.a1, "--to", self.a2, "short")
        self.assertEqual(self.last()[0].message, "short")
        rc, out, _ = self.cli("post", "--job", "J", "--as", self.a1, "x " * 400)
        self.assertIn("truncated", out)
        self.assertFalse((self.share / "posts").exists())


if __name__ == "__main__":
    unittest.main()
