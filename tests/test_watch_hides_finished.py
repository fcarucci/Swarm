"""Francesco 2026-10-17: swarm watch and status --job hide finished agents by default (--all-agents shows them)."""
import datetime as dt
import unittest
from types import SimpleNamespace

import support  # noqa: F401  (sets sys.path)
from swarm import cli  # noqa: E402


def _a(status, ended_s=5):
    now = dt.datetime.now(dt.timezone.utc)
    end = now - dt.timedelta(seconds=ended_s)
    return SimpleNamespace(status=status, ended_at=end if status in cli.FINISHED else None, last_contact_at=end)


class WatchHidesFinishedTests(unittest.TestCase):
    def test_default_window_hides_every_finished_agent(self):
        self.assertEqual(cli.DEFAULTS["board"]["watch_recent_minutes"], 0)
        rows = [_a("running"), _a("completed", 1), _a("left", 2), _a("dead", 3), _a("idle")]
        shown, hidden = cli._recent_agents(rows, dt.datetime.now(dt.timezone.utc), 0)
        self.assertEqual([a.status for a in shown], ["running", "idle"])
        self.assertEqual(hidden, 3)

    def test_all_agents_shows_everyone(self):
        rows = [_a("running"), _a("completed", 1)]
        shown, hidden = cli._recent_agents(rows, dt.datetime.now(dt.timezone.utc), None)
        self.assertEqual(len(shown), 2); self.assertEqual(hidden, 0)


if __name__ == "__main__":
    unittest.main()
