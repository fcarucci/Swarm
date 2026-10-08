"""External events (schema 21): the generic "something happened, wake the orchestrator" record.

A listener or poller posts an event (idempotent per job+kind+key); the orchestrator, an agent or
a role sees it in its hook output until it acks it; `swarm event wait` blocks until one is
pending. The contract runs on every backend (Postgres needs SWARM_TEST_CONFIG, as elsewhere)."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import unittest

from support import (FileHarness, MemoryHarness, PostgresHarness, SqliteHarness,  # noqa: F401  (sets sys.path)
                     wait_until)

from swarm.board import ReadOnlyBoard
from swarm.board.base import SCHEMA_VERSION


class EventContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", "events", None, None, None)
        self.b.open_job("k", "other", None, None, None)

    def test_schema_is_21(self):
        self.assertEqual(SCHEMA_VERSION, 21)
        self.assertEqual(type(self.b).schema_version(self.h.cfg), 21)

    def test_post_is_idempotent_per_job_kind_key(self):
        first, created = self.b.post_event("j", "NEEDS-REVIEW", "7@abc", "PR 7 opened", source="gitea")
        self.assertTrue(created)
        again, created = self.b.post_event("j", "NEEDS-REVIEW", "7@abc", "PR 7 opened again")
        self.assertEqual((again, created), (first, False))
        self.assertEqual(len(self.b.events("j")), 1)
        self.assertEqual(self.b.events("j")[0].text, "PR 7 opened")   # the first one stands
        other_key, c1 = self.b.post_event("j", "NEEDS-REVIEW", "7@def", "new head")
        other_kind, c2 = self.b.post_event("j", "CI-FAILED", "7@abc", "ci")
        other_job, c3 = self.b.post_event("k", "NEEDS-REVIEW", "7@abc", "same key, other job")
        self.assertTrue(c1 and c2 and c3)
        self.assertEqual(len({first, other_key, other_kind, other_job}), 4)

    def test_event_record_fields(self):
        before = self.b.now()
        eid, _ = self.b.post_event("j", "READY-TO-LAND", "7@abc", "PR 7 ready", to="@PM", source="gitea")
        (ev,) = self.b.events("j")
        self.assertEqual((ev.id, ev.job, ev.kind, ev.key, ev.to, ev.text, ev.source),
                         (eid, "j", "READY-TO-LAND", "7@abc", "@pm", "PR 7 ready", "gitea"))
        self.assertIsNone(ev.acked_at)
        self.assertIsNone(ev.acked_by)
        self.assertGreaterEqual(ev.created_at, before - dt.timedelta(seconds=5))
        eid2, _ = self.b.post_event("j", "X", "1", "no target")
        self.assertIsNone(self.b.events("j")[1].to)
        self.assertIsNone(self.b.events("j")[1].source)

    def test_pending_ack_and_history(self):
        a, _ = self.b.post_event("j", "A", "1", "one")
        b, _ = self.b.post_event("j", "B", "1", "two")
        self.assertEqual([e.id for e in self.b.pending_events("j")], [a, b])   # oldest first
        self.assertEqual(self.b.ack_events("j", [a], "Alice"), 1)
        self.assertEqual([e.id for e in self.b.pending_events("j")], [b])
        acked = [e for e in self.b.events("j") if e.id == a][0]
        self.assertEqual(acked.acked_by, "Alice")
        self.assertIsNotNone(acked.acked_at)
        self.assertEqual(len(self.b.events("j")), 2)                          # history stays
        self.assertEqual(self.b.ack_events("j", [a], "Bob"), 0)                # already acked: no-op
        self.assertEqual([e for e in self.b.events("j") if e.id == a][0].acked_by, "Alice")
        self.assertEqual(self.b.ack_events("j", [a, b, 99999], "Bob"), 1)      # unknown ids ignored
        self.assertEqual(self.b.pending_events("j"), [])

    def test_an_acked_event_is_not_reposted(self):
        a, _ = self.b.post_event("j", "A", "1", "one")
        self.b.ack_events("j", [a], "Alice")
        again, created = self.b.post_event("j", "A", "1", "one")
        self.assertEqual((again, created), (a, False))
        self.assertEqual(self.b.pending_events("j"), [])

    def test_ack_is_per_job(self):
        a, _ = self.b.post_event("j", "A", "1", "one")
        self.assertEqual(self.b.ack_events("k", [a], "Alice"), 0)
        self.assertEqual(len(self.b.pending_events("j")), 1)

    def test_jobs_are_separate(self):
        self.b.post_event("j", "A", "1", "one")
        self.b.post_event("k", "A", "1", "other")
        self.assertEqual([e.text for e in self.b.pending_events("k")], ["other"])

    def test_target_filter(self):
        orch, _ = self.b.post_event("j", "A", "orch", "for the orchestrator")
        pm, _ = self.b.post_event("j", "A", "pm", "for the pm", to="@pm")
        el, _ = self.b.post_event("j", "A", "el", "for the el", to="@EL")
        bob, _ = self.b.post_event("j", "A", "bob", "for bob", to="Bob")
        ids = lambda **kw: [e.id for e in self.b.pending_events("j", **kw)]   # noqa: E731
        self.assertEqual(ids(), [orch, pm, el, bob])                  # no filter: everything
        self.assertEqual(ids(to="@pm"), [orch, pm])                   # the PM seat also hears the orchestrator's
        self.assertEqual(ids(to="@PM"), [orch, pm])                   # roles compare case-insensitively
        self.assertEqual(ids(to="@el"), [el])
        self.assertEqual(ids(to="Bob"), [bob])
        self.assertEqual(ids(to="bob"), [])                           # agent names are exact
        self.assertEqual(ids(to=("Bob", "@el")), [el, bob])           # several targets at once
        self.assertEqual(ids(to=(None,)), [orch])                     # None: addressed to nobody in particular

    def test_validation(self):
        bad = [dict(kind="needs review", key="1", text="t"), dict(kind="", key="1", text="t"),
               dict(kind="A", key="", text="t"), dict(kind="A", key="x" * 201, text="t"),
               dict(kind="A", key="1", text=""), dict(kind="A", key="1", text="t", to="@"),
               dict(kind="A", key="1", text="t", to="bad\nname"), dict(kind="A", key="1", text="t", source="a\nb")]
        for kw in bad:
            with self.subTest(**kw), self.assertRaises(ValueError):
                self.b.post_event("j", **kw)
        with self.assertRaises(ValueError):
            self.b.post_event("nosuch", "A", "1", "t")
        self.assertEqual(self.b.events("j"), [])

    def test_text_is_one_clean_line_of_at_most_500_chars(self):
        self.b.post_event("j", "A", "1", "line one\n\x1b[2Jline two\t" + "x" * 600)
        text = self.b.events("j")[0].text
        self.assertLessEqual(len(text), 500)
        self.assertNotIn("\n", text)
        self.assertNotIn("\x1b", text)
        self.assertTrue(text.startswith("line one [2Jline two x"))

    def test_wait_returns_at_once_when_something_is_pending(self):
        a, _ = self.b.post_event("j", "A", "1", "one", to="@pm")
        t0 = time.monotonic()
        got = self.b.wait_event("j", to="@pm", timeout=30)
        self.assertEqual([e.id for e in got], [a])
        self.assertLess(time.monotonic() - t0, 3)

    def test_wait_times_out_with_nothing(self):
        self.b.post_event("j", "A", "1", "for bob", to="Bob")
        t0 = time.monotonic()
        self.assertEqual(self.b.wait_event("j", to="@pm", timeout=0.4), [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)

    def test_wait_wakes_when_another_process_posts(self):
        poster = self.h.board()
        self.addCleanup(poster.close)
        out = []
        t = threading.Thread(target=lambda: out.append(self.b.wait_event("j", to="@pm", timeout=20)))
        t.start()
        time.sleep(0.5)
        self.assertTrue(t.is_alive())
        poster.post_event("j", "A", "unrelated", "for bob", to="Bob")   # not for the waiter
        time.sleep(0.3)
        self.assertTrue(t.is_alive())
        started = time.monotonic()
        poster.post_event("j", "A", "1", "for the pm", to="@pm")
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([e.text for e in out[0]], ["for the pm"])

    def test_purge_drops_old_acked_events_only(self):
        old, _ = self.b.post_event("j", "A", "old", "acked long ago")
        keep, _ = self.b.post_event("j", "A", "pending", "still pending")
        fresh, _ = self.b.post_event("j", "A", "fresh", "acked now")
        self.b.ack_events("j", [old, fresh], "Alice")
        self.h.backdate_event(old, 30 * 86400)
        self.h.backdate_event(keep, 30 * 86400)
        self.b.purge()
        self.assertEqual(sorted(e.id for e in self.b.events("j")), sorted([keep, fresh]))

    def test_a_read_only_board_refuses_writes(self):
        from swarm.board import open_read_only
        if self.h.name not in ("file", "sqlite"):
            self.skipTest("only the file and SQLite backends open read-only")
        ro = open_read_only(self.h.cfg)
        self.addCleanup(ro.close)
        with self.assertRaises(ReadOnlyBoard):
            ro.post_event("j", "A", "1", "t")
        with self.assertRaises(ReadOnlyBoard):
            ro.ack_events("j", [1], "x")
        self.assertEqual(ro.pending_events("j"), [])


class MemoryEvents(EventContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteEvents(EventContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileEvents(EventContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway Postgres board config")
class PostgresEvents(EventContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway Postgres board config")
class PostgresEventWake(unittest.TestCase):
    """The waiter is woken by NOTIFY (no polling): a post wakes it well inside a poll tick."""

    def test_notify_wakes_a_waiter_promptly_and_listen_precedes_the_check(self):
        h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])
        self.addCleanup(h.close)
        h.reset()
        waiter, poster = h.board(), h.board()
        self.addCleanup(waiter.close)
        self.addCleanup(poster.close)
        waiter.open_job("j", None, None, None, None)
        out = []
        t = threading.Thread(target=lambda: out.append(waiter.wait_event("j", timeout=20)))
        t.start()
        wait_until(lambda: getattr(waiter, "_listening_events", False), timeout=10)
        started = time.monotonic()
        poster.post_event("j", "A", "1", "wake")
        t.join(10)
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(len(out[0]), 1)


if __name__ == "__main__":
    unittest.main()
