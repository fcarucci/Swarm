"""`watch` and `status --job` hide finished agents older than board.watch_recent_minutes."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from support import ROOT, posix_only  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from test_hooks_cli import Env  # noqa: E402

UTC = dt.timezone.utc


def fake(status, last_contact, ended=None):
    return SimpleNamespace(status=status, last_contact_at=last_contact, ended_at=ended)


class RecentAgentsTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    def ago(self, **kw):
        return self.NOW - dt.timedelta(**kw)

    def test_window_boundary_is_inclusive(self):
        edge = fake("completed", self.ago(hours=1), ended=self.ago(minutes=10))
        past = fake("completed", self.ago(hours=1), ended=self.ago(minutes=10, seconds=1))
        shown, hidden = swarm._recent_agents([edge, past], self.NOW, 10)
        self.assertEqual((shown, hidden), ([edge], 1))

    def test_active_always_shown_finished_by_end_or_last_contact(self):
        rows = [fake("running", self.ago(days=1)), fake("idle", self.ago(days=1)),
                fake("started", self.ago(days=1)),
                fake("left", self.ago(minutes=2), ended=self.ago(hours=2)),     # recent contact
                fake("dead", self.ago(minutes=31)),                              # no end, old contact
                fake("completed", self.ago(hours=3), ended=self.ago(minutes=3))]
        shown, hidden = swarm._recent_agents(rows, self.NOW, 10)
        self.assertEqual(shown, [rows[0], rows[1], rows[2], rows[3], rows[5]])
        self.assertEqual(hidden, 1)

    def test_none_shows_everyone(self):
        rows = [fake("completed", self.ago(days=3), ended=self.ago(days=3))]
        self.assertEqual(swarm._recent_agents(rows, self.NOW, None), (rows, 0))


class HiddenAgentsCliTests(Env):
    """Four agents on J: one active, one completed 2 minutes ago, two completed 11 minutes ago."""

    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J")
        for key in ("live", "fresh", "old1", "old2"):
            self.cli("join", "--job", "J", "--key", key)
        now = dt.datetime.now(UTC)
        for key, minutes in (("fresh", 2), ("old1", 11), ("old2", 11)):
            self.h.update_agent(key, state="completed", left_at=now - dt.timedelta(minutes=minutes),
                                last_seen=now - dt.timedelta(minutes=minutes + 5))
        with self.board() as b:
            self.names = {a.agent_key: a.name for a in b.agents("J")}

    def assert_listed(self, out, *keys, absent=()):
        for k in keys:
            self.assertIn(self.names[k], out)
        for k in absent:
            self.assertNotIn(self.names[k], out)

    def test_status_hides_old_finished_with_hint(self):
        _, out, _ = self.cli("status", "--job", "J")
        self.assert_listed(out, "live", "fresh", absent=("old1", "old2"))
        self.assertTrue(out.endswith("\n(2 older finished agents hidden · --all-agents to show)\n"), out)

    def test_status_all_agents_shows_everyone_without_hint(self):
        _, out, _ = self.cli("status", "--job", "J", "--all-agents")
        self.assert_listed(out, "live", "fresh", "old1", "old2")
        self.assertNotIn("hidden", out)

    def test_custom_window_and_default(self):
        with self.board() as b:
            out = swarm.agents_table(b, "J", False, b.now(), 1, "x")
        self.assert_listed(out, "live", absent=("fresh", "old1", "old2"))
        self.assertTrue(out.endswith("\n(3 older finished agents hidden · x)"))
        self.assertEqual(swarm.load_config(self.config)["board"]["watch_recent_minutes"], 10)

    def test_order_and_columns_unchanged(self):
        _, shown, _ = self.cli("status", "--job", "J", "--all-agents")
        _, hidden, _ = self.cli("status", "--job", "J")
        # Compare cells, not padding: names are random, so a hidden agent with a longer name
        # widens the --all-agents table's columns (a flaky failure about 1 run in 10).
        cells = lambda lines: [ln.split() for ln in lines]
        table = [ln for ln in hidden.splitlines() if "hidden" not in ln]
        self.assertEqual(cells(table), cells([ln for ln in shown.splitlines()
                                              if not any(self.names[k] in ln for k in ("old1", "old2"))]))

    def test_agents_table_shows_host_and_model(self):
        with self.board() as b:
            b.set_agent_runtime("live", "codex", "gpt-5.5-codex")
            out = swarm.agents_table(b, "J", False, b.now())
        self.assertIn("HOST", out.splitlines()[0])
        self.assertIn("MODEL", out.splitlines()[0])
        self.assertIn("codex", out)
        self.assertIn("gpt-5.5-codex", out)

    def test_short_model(self):
        self.assertEqual(swarm._short_model("claude-opus-5-5"), "opus-5-5")
        self.assertEqual(swarm._short_model("abcdefghijklmnopqrst"), "abcdefghijklmnopqr")
        self.assertEqual(swarm._short_model(None), "")

    def frame(self, view, job="J"):
        with self.board() as b, mock.patch("shutil.get_terminal_size",
                                           return_value=os.terminal_size((200, 60))):
            return "\n".join(swarm._watch_frame(b, job, 2.0, False, True, view))

    def test_watch_hides_and_a_toggles(self):
        view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False, "recent_minutes": 10}
        for job in ("J", None):
            out = self.frame(view, job)
            self.assert_listed(out, "live", "fresh", absent=("old1", "old2"))
            self.assertIn("\n(2 older finished agents hidden · press a to show all)\n", out)
            self.assertIn("a all agents", out.splitlines()[0])
        self.assertTrue(swarm._apply_keys("a", view))
        self.assertTrue(view["all_agents"])
        out = self.frame(view)
        self.assert_listed(out, "live", "fresh", "old1", "old2")
        self.assertNotIn("hidden", out)
        swarm._apply_keys("a", view)
        self.assertFalse(view["all_agents"])
        self.assertIn("2 older finished agents hidden", self.frame(view))

    def test_v_hides_every_agents_table_and_shows_them_again(self):
        view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False, "recent_minutes": 10}
        self.assertIn("v hide agents", self.frame(view, None).splitlines()[0])
        self.assertTrue(swarm._apply_keys("v", view))
        self.assertTrue(view["hide_agents"])
        for job in ("J", None):
            out = self.frame(view, job)
            self.assert_listed(out, absent=("live", "fresh", "old1", "old2"))
            self.assertNotIn("AGENTS ·", out)
            self.assertIn("\n(agents hidden · v to show)\n", out)
            self.assertIn("MESSAGES", out)
        self.assertIn("v show agents", self.frame(view, None).splitlines()[0])
        self.assertIn("JOBS", self.frame(view, None))       # the job table stays
        swarm._apply_keys("v", view)
        self.assertFalse(view["hide_agents"])
        self.assert_listed(self.frame(view), "live", "fresh")

    def test_hint_is_dim_in_colour(self):
        with self.board() as b:
            out = swarm.agents_table(b, "J", True, b.now(), 10, "press a to show all")
        self.assertTrue(out.endswith("\n\033[2m(2 older finished agents hidden · press a to show all)\033[0m"))

    def test_singular(self):
        self.h.update_agent("old2", left_at=dt.datetime.now(UTC))
        _, out, _ = self.cli("status", "--job", "J")
        self.assertIn("(1 older finished agent hidden · --all-agents to show)", out)



class ControlsInTablesTests(Env):
    """`who`, `status` and `status --job` show board text with its controls escaped."""

    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J", "--description", "d", "--goal", "g")
        self.cli("join", "--job", "J", "--key", "a1")
        self.cli("wait", "--job", "J", "--on", "x")

    def run_with(self, fn, *args):
        import contextlib
        import io
        from test_watch_scroll import Tainted
        with Tainted(self.board()) as b, contextlib.redirect_stdout(io.StringIO()) as out:
            res = fn(b, *args)
        return (res if isinstance(res, str) else "") + out.getvalue()

    def assert_clean(self, text):
        from test_watch_scroll import FORBIDDEN
        self.assertIsNone(FORBIDDEN.search(text), repr(text))
        self.assertIn(r"\x1b]0;pwn\x07", text)
        self.assertFalse(any(ln.startswith("[swarm] forged") for ln in text.splitlines()))

    def test_who(self):
        out = self.run_with(swarm._board_who, self.cfg, SimpleNamespace(job="J"))
        self.assertEqual(len(out.splitlines()), 1)
        self.assert_clean(out)

    def test_status_overview_and_job(self):
        self.assert_clean(self.run_with(swarm.jobs_overview, True, False))
        self.assert_clean(self.run_with(swarm.job_detail, "J", False))
        from test_watch_scroll import OUR_SGR
        self.assert_clean(self.run_with(lambda b: OUR_SGR.sub("", swarm.job_detail(b, "J", True))))


if __name__ == "__main__":
    unittest.main()


class MarkerReadTests(Env):
    """The marker dir is sandbox-writable. What the CLI reads there (cli._read_marker, and
    the marker lock) never blocks on a FIFO, never follows a link, and skips hard links and
    oversized files."""

    def setUp(self):
        super().setUp()
        self.markers.mkdir(mode=0o700, exist_ok=True)

    def fifo(self, name):
        path = self.markers / name
        os.mkfifo(path)

        def unblock():   # a reader stuck in open() is released by a writer opening the FIFO
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        self.addCleanup(unblock)
        return path

    def within(self, seconds, fn, *args):
        import threading
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("r", fn(*args)), daemon=True)
        t.start()
        t.join(seconds)
        self.assertFalse(t.is_alive(), f"{fn.__name__} blocked")
        return box.get("r")

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_fifo_marker_is_not_read(self):
        self.assertEqual(self.within(1.0, swarm._read_marker, self.fifo("J.json")), {})

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_status_and_deactivate_with_a_fifo_marker(self):
        self.cli("activate", "--job", "K")
        self.fifo("J.json")
        self.fifo("K--x.json")
        rc, _, _ = self.within(2.0, self.cli, "status")
        self.assertEqual(rc, 0)
        rc, _, err = self.within(2.0, self.cli, "deactivate", "--job", "K")
        self.assertEqual(rc, 0, err)

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_fifo_at_the_jobs_own_marker_name(self):
        self.fifo("J.json")
        rc, _, err = self.within(2.0, self.cli, "deactivate", "--job", "J")
        self.assertEqual(rc, 0, err)

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_locked_marker_on_a_fifo_or_link(self):
        fifo = self.fifo("J.json")
        outside = self.tmp / "outside.json"
        outside.write_text('{"job": "J"}')
        link = self.markers / "L.json"
        link.symlink_to(outside)

        def lock(path):
            try:
                with swarm.locked_marker(path, 0.1):
                    return "locked"
            except FileNotFoundError:
                return "none"
        self.assertEqual(self.within(1.0, lock, fifo), "none")
        self.assertEqual(self.within(1.0, lock, link), "none")

    def test_links_and_oversized_markers_are_ignored(self):
        outside = self.tmp / "outside.json"
        outside.write_text('{"job": "J"}')
        (self.markers / "sym.json").symlink_to(outside)
        os.link(outside, self.markers / "hard.json")
        (self.markers / "big.json").write_text('{"job": "J", "pad": "' + "x" * 70000 + '"}')
        (self.markers / "list.json").write_text('["job"]')
        (self.markers / "ok.json").write_text('{"job": "J"}')
        for name in ("sym.json", "hard.json", "big.json", "list.json"):
            self.assertEqual(swarm._read_marker(self.markers / name), {}, name)
        self.assertEqual(swarm._read_marker(self.markers / "ok.json"), {"job": "J"})


class BadNameTests(Env):
    """The board refuses a name that fails valid_name with ValueError; `post` and
    `verdict` report it and exit 1 instead of a traceback."""

    def test_post_and_verdict_report_a_refused_name(self):
        import contextlib
        import io

        class Refusing:
            def post(self, *a, **kw):
                raise ValueError("invalid name: 'a\\nb'")

            def record_verdict(self, *a, **kw):
                raise ValueError("invalid name")

            def __getattr__(self, name):
                raise AttributeError(name)

        for fn, args in ((swarm._board_post, SimpleNamespace(job="J", name="a\nb", message=["hi"], to=None)),
                         (swarm._board_verdict, SimpleNamespace(job="J", name="a\nb", verdict="met", reason=["x"]))):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch("swarm.spool.deliver_verdict",
                                                             side_effect=ValueError("invalid name")):
                rc = fn(Refusing(), self.cfg, args)
            self.assertEqual(rc, 1)
            self.assertIn("invalid name", err.getvalue())
            self.assertNotIn("\n", err.getvalue().strip())
