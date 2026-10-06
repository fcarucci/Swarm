"""Backend-agnostic contract tests for board.Board.

BoardContract is a mixin: a concrete TestCase sets `harness_factory` to something returning a
harness (see support.py). It always runs against MemoryBoard. It runs against Postgres only when
$SWARM_TEST_CONFIG names a config file for a THROWAWAY database (every table is truncated).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import threading
import unittest
from unittest import mock

from support import wait_until, SMALL_POOL, FileHarness, fake_image, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)

from swarm import compat  # noqa: E402
from swarm.board import (MEMORY_SEEN_MAX, AgentEvent, AgentStatus, BoardError, JobStatus,  # noqa: E402
                   Member, MemoryRef, Message, OwedReply, PostResult, ReadResult, Route, RosterEntry,
                   SetupResult, SyncState, TranscriptImage, TranscriptRow, TranscriptSummary)

MIN = 60
DAY = 86400


class BoardContract:
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
        self.test_started = self.b.now()
        self.addCleanup(self.b.close)

    # ---- helpers
    def status_of(self, key: str, job: str = "j") -> str:
        return next(a.status for a in self.b.agents(job) if a.agent_key == key)

    def agent(self, key: str, job: str = "j") -> AgentStatus:
        return next(a for a in self.b.agents(job) if a.agent_key == key)

    # ---- agent host / model / Codex turn ends
    def test_custom_role_update_preserves_identity_and_protected_roles(self):
        name = self.b.allocate_name("k1", "j", "general-purpose")
        self.b.set_agent_role("k1", "product_manager")
        self.assertEqual((self.agent("k1").name, self.agent("k1").role), (name, "product_manager"))
        self.assertEqual(self.b.roster("j")[0].role, "product_manager")
        self.b.claim_verifier("k1", "j")
        self.b.set_agent_role("k1", "qa")
        self.assertEqual(self.agent("k1").role, "verifier")
        for reserved in ("judge", "verifier"):
            with self.assertRaises(ValueError):
                self.b.set_agent_role("k1", reserved)
        self.b.allocate_name("k2", "j", "engineer")
        self.b.agent_stopped("k2")
        self.b.set_agent_role("k2", "qa")
        self.b.set_agent_role("missing", "qa")
        self.assertEqual(self.agent("k2").role, "engineer")

    def test_set_agent_runtime_on_active_agent_only(self):
        self.b.allocate_name("k1", "j")
        self.b.set_agent_runtime("k1", "codex", "gpt-x")
        self.b.set_agent_runtime("k1", None, None)
        self.assertEqual((self.agent("k1").harness, self.agent("k1").model), ("codex", "gpt-x"))
        self.b.agent_stopped("k1")
        self.b.set_agent_runtime("k1", "claude", "opus")
        self.assertEqual((self.agent("k1").harness, self.agent("k1").model), ("codex", "gpt-x"))

    def test_turn_ended_then_quiet_finishes_agent(self):
        self.b.allocate_name("k1", "j")
        self.b.allocate_name("k2", "j")
        self.b.tool_started("k1", "Bash")
        self.b.agent_turn_ended("k1")
        self.assertIsNone(self.agent("k1").current_tool)
        self.assertEqual(self.b.finish_quiet_agents(3600), [])
        self.h.backdate_agent("k1", turn_ended_at=120)
        self.assertEqual(self.b.finish_quiet_agents(60), ["k1"])
        self.assertEqual(self.agent("k1").status, "completed")
        self.assertIsNone(self.agent("k2").ended_at)
        self.assertEqual(self.b.finish_quiet_agents(60), [])

    def test_tool_started_cancels_turn_end(self):
        self.b.allocate_name("k1", "j")
        self.b.agent_turn_ended("k1")
        self.h.backdate_agent("k1", turn_ended_at=120)
        self.b.tool_started("k1", "Bash")
        self.assertEqual(self.b.finish_quiet_agents(60), [])
        self.assertIsNone(self.agent("k1").ended_at)

    def test_turns_resumed_restarts_the_quiet_window_of_the_job_only(self):
        self.b.open_job("other", None, None, None, "me")
        for k, job in (("k1", "j"), ("k2", "j"), ("k3", "j"), ("k4", "other")):
            self.b.allocate_name(k, job)
        for k in ("k1", "k2", "k4"):
            self.b.agent_turn_ended(k)
            self.h.backdate_agent(k, turn_ended_at=120)
        # a follow-up in job j: its ended agents' windows restart now (k3 had no turn end)
        self.assertEqual(sorted(self.b.turns_resumed("j")), ["k1", "k2"])
        self.assertIsNone(self.agent("k3").ended_at)
        self.assertEqual(self.b.finish_quiet_agents(60), ["k4"])      # nobody in j is quiet yet
        self.b.tool_started("k1", "Bash")                             # the child that got the follow-up
        # once the window has passed (quiet_seconds -1: every set turn end counts as quiet), the
        # sibling that got no further turn completes: its turn end was restarted, not cleared
        self.assertEqual(self.b.finish_quiet_agents(-1), ["k2"])
        self.assertIsNone(self.agent("k1").ended_at)
        self.assertEqual(self.b.turns_resumed("j"), [])
        self.b.agent_turn_ended("k1")                                 # its next turn end re-arms it
        self.h.backdate_agent("k1", turn_ended_at=120)
        self.assertEqual(self.b.finish_quiet_agents(60), ["k1"])

    def test_transcript_harness_round_trip(self):
        from dataclasses import replace
        from swarm.transcripts import make_row
        self.b.ensure_job("j")
        row = make_row("j", "k1", "Homer Simpson", "subagent", '{"a":1}\n', host="box")
        self.b.save_transcript(replace(row, harness="codex"))
        self.assertEqual(self.b.transcripts(job="j")[0].harness, "codex")

    def test_os_user_and_member_model(self):
        import getpass
        self.b.allocate_name("k1", "j")
        self.assertEqual(self.agent("k1").os_user, getpass.getuser())
        self.assertIsNone(self.b.tool_started("k1", "Bash").model)
        self.b.set_agent_runtime("k1", None, "m1")
        self.assertEqual(self.b.tool_started("k1", "Bash").model, "m1")

    def test_refresh_transcript_updates_time_and_only_upgrades_final(self):
        from swarm.transcripts import make_row
        past = self.b.now() - dt.timedelta(hours=1)
        self.b.save_transcript(make_row("j", "k1", "Homer Simpson", "subagent", '{"a":1}\n', final=False, captured_at=past))
        self.assertTrue(self.b.refresh_transcript("j", "k1", False))
        row = self.b.transcripts(job="j")[0]
        self.assertGreater(row.captured_at, past + dt.timedelta(minutes=30))
        self.assertFalse(row.final)
        self.b.refresh_transcript("j", "k1", True)
        self.b.refresh_transcript("j", "k1", False)
        self.assertTrue(self.b.transcripts(job="j")[0].final)
        self.assertFalse(self.b.refresh_transcript("j", "nobody", True))

    def test_pending_final_transcripts_lists_owned_ended_agents_missing_or_unfinal(self):
        import getpass
        from swarm.transcripts import make_row
        me, host = getpass.getuser(), compat.node()
        for k in ("ended", "norow", "active", "final", "other_user", "claude_one"):
            self.b.allocate_name(k, "j")
            self.b.set_agent_runtime(k, "claude" if k == "claude_one" else "codex", "m")
            if k != "norow":
                self.b.save_transcript(make_row("j", k, k, "subagent", '{"a":1}\n', final=(k == "final")))
        self.h.update_agent("other_user", os_user="someone-else")
        for k in ("ended", "norow", "final", "other_user", "claude_one"):
            self.b.agent_stopped(k)
        since = self.b.now() - dt.timedelta(days=1)
        self.assertEqual(sorted(self.b.pending_final_transcripts(host, me, "codex", since)),
                         [("j", "ended"), ("j", "norow")])
        self.assertEqual(self.b.pending_final_transcripts(host, me, "codex", self.b.now() + dt.timedelta(minutes=1)), [])

    # ---- lifecycle
    def test_now_is_timezone_aware(self):
        now = self.b.now()
        self.assertIsNotNone(now.tzinfo)
        self.assertGreaterEqual(now, self.test_started)
        self.assertLessEqual(now, self.b.now())

    def test_setup_is_idempotent_and_counts_pool(self):
        res = type(self.b).setup(self.h.cfg, SMALL_POOL)
        self.assertIsInstance(res, SetupResult)
        self.assertEqual(res.pool, {"simpsons": 3, "english": 2})
        res = type(self.b).setup(self.h.cfg, {"simpsons": ["Homer Simpson", "Lisa Simpson"], "english": []})
        self.assertEqual(res.pool, {"simpsons": 4, "english": 2})

    def test_context_manager_and_close_idempotent(self):
        with self.h.board() as b:
            b.ensure_job("j")
        b.close()
        b.close()

    # ---- names
    def test_allocate_is_idempotent_per_key(self):
        n1 = self.b.allocate_name("k1", "j", "worker")
        self.assertEqual(self.b.allocate_name("k1", "j", "other"), n1)
        self.assertEqual(self.b.active_agent_name("k1"), n1)
        self.assertEqual(self.agent("k1").role, "worker")

    def test_allocate_active_key_moves_job(self):
        n1 = self.b.allocate_name("k1", "j")
        self.assertEqual(self.b.allocate_name("k1", "j2"), n1)
        self.assertEqual([a.agent_key for a in self.b.agents("j2")], ["k1"])
        self.assertEqual(self.b.agents("j"), [])

    def test_names_simpsons_then_english_then_suffixed(self):
        names = [self.b.allocate_name(f"k{i}", "j") for i in range(7)]
        self.assertEqual(len(set(names)), 7)
        self.assertEqual(set(names[:3]), set(SMALL_POOL["simpsons"]))
        self.assertEqual(set(names[3:5]), set(SMALL_POOL["english"]))
        for n in names[5:]:
            self.assertRegex(n, r"^\S.* \d{3}$")
            self.assertTrue(100 <= int(n.rsplit(" ", 1)[1]) <= 999)

    def test_departed_name_is_free_again(self):
        self.h.reset({"simpsons": ["Homer Simpson"], "english": []})
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")
        self.assertNotEqual(self.b.allocate_name("k2", "j"), "Homer Simpson")
        self.b.agent_stopped("k1")
        self.assertEqual(self.b.allocate_name("k3", "j"), "Homer Simpson")

    def test_concurrent_allocation_gives_unique_names(self):
        n = 12
        results, errors = {}, []
        barrier = threading.Barrier(n, timeout=120)

        def worker(i):
            try:
                with self.h.board() as b:
                    barrier.wait()
                    results[i] = b.allocate_name(f"ck{i}", "j")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), n)
        self.assertEqual(len(set(results.values())), n, results)
        self.assertTrue(set(SMALL_POOL["simpsons"]) <= set(results.values()))
        self.assertEqual(sorted(a.name for a in self.b.agents("j", include_departed=False)),
                         sorted(results.values()))

    def test_revive_keeps_name_cursor_and_counters(self):
        name = self.b.allocate_name("k1", "j", "worker")
        self.b.allocate_name("other", "j")
        first = self.b.post("j", "Someone", "first").id
        self.b.tool_started("k1", "Bash")
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1")], [first])
        self.b.agent_stopped("k1")
        self.assertIsNone(self.b.active_agent_name("k1"))
        second = self.b.post("j", "Someone", "second").id
        self.assertEqual(self.b.allocate_name("k1", "j"), name)
        a = self.agent("k1")
        self.assertEqual((a.status, a.tool_calls, a.role, a.ended_at, a.current_tool),
                         ("started", 1, "worker", None, None))
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1")], [second])

    def test_revive_when_name_taken_gets_new_name_and_fresh_cursor(self):
        self.h.reset({"simpsons": ["Homer Simpson"], "english": []})
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")
        m = self.b.post("j", "Someone", "history").id
        self.b.read_new(agent_key="k1")
        self.b.agent_stopped("k1")
        self.assertEqual(self.b.allocate_name("k2", "j"), "Homer Simpson")
        new = self.b.allocate_name("k1", "j")
        self.assertNotEqual(new, "Homer Simpson")
        self.assertEqual([x.id for x in self.b.read_new(agent_key="k1")], [m])  # cursor reset to 0

    def test_was_member_and_active_agent_name(self):
        name = self.b.allocate_name("k1", "j")
        self.assertTrue(self.b.was_member("k1", "j"))
        self.assertFalse(self.b.was_member("k1", "other"))
        self.assertFalse(self.b.was_member("nobody", "j"))
        self.b.agent_stopped("k1")
        self.assertTrue(self.b.was_member("k1", "j"))
        self.assertIsNone(self.b.active_agent_name("k1"))
        self.assertIsNone(self.b.active_agent_name("nobody"))
        self.assertTrue(name)

    # ---- posting
    def test_post_normalises_whitespace(self):
        r = self.b.post("j", "A", "  hello \n\t  world  ")
        self.assertIsInstance(r, PostResult)
        self.assertFalse(r.truncated)
        self.assertEqual(self.b.recent_messages(1, "j")[0].message, "hello world")

    def test_post_cap_and_truncation_marker(self):
        cap = int(self.h.cfg["board"]["message_max_chars"])
        exact = self.b.post("j", "A", "x" * cap)
        self.assertFalse(exact.truncated)
        long = self.b.post("j", "A", "é" * (cap + 50))  # characters, not bytes
        self.assertTrue(long.truncated)
        msgs = {m.id: m.message for m in self.b.recent_messages(10, "j")}
        self.assertEqual(msgs[exact.id], "x" * cap)
        self.assertEqual(len(msgs[long.id]), cap)
        self.assertTrue(msgs[long.id].endswith("…"))
        self.assertEqual(msgs[long.id][:-1], "é" * (cap - 1))
        self.assertFalse(self.b.post("j", "A", "é" * cap).truncated)

    def test_post_empty_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.b.post("j", "A", " \n\t ")

    def test_post_creates_job_and_ids_increase_across_jobs(self):
        a = self.b.post("new-job", "A", "one").id
        b = self.b.post("other-job", "A", "two").id
        c = self.b.post("new-job", "A", "three").id
        self.assertTrue(0 < a < b < c)
        self.assertIsNotNone(self.b.job_status("new-job"))
        self.assertEqual(self.b.last_message_id(), c)
        self.assertEqual(self.b.last_message_id("other-job"), b)
        self.assertEqual(self.b.last_message_id("empty"), 0)

    def test_post_updates_poster_last_post(self):
        name = self.b.allocate_name("k1", "j")
        self.h.backdate_agent("k1", last_seen=10 * MIN)
        self.b.post("j", name, "hi", to="Somebody")
        a = self.agent("k1")
        self.assertIsNotNone(a.last_post_at)
        self.assertGreaterEqual(a.last_contact_at, self.test_started)
        self.assertLessEqual(a.last_contact_at, self.b.now())
        self.assertEqual(a.messages, 1)
        m = self.b.recent_messages(1, "j")[0]
        self.assertIsInstance(m, Message)
        self.assertEqual((m.job, m.agent_name, m.to_agent), ("j", name, "Somebody"))

    # ---- reading
    def test_read_new_excludes_own_and_advances(self):
        me = self.b.allocate_name("k1", "j")
        self.b.post("j", me, "mine")
        o1 = self.b.post("j", "Other", "one").id
        self.b.post("x", "Other", "other job")
        rows = self.b.read_new(agent_key="k1")
        self.assertEqual([m.id for m in rows], [o1])
        self.assertEqual(self.b.read_new(agent_key="k1"), [])
        o2 = self.b.post("j", "Other", "two").id
        self.assertEqual([m.id for m in self.b.read_new(name=me)], [o2])

    def test_own_posts_skipped_for_good(self):
        me = self.b.allocate_name("k1", "j")
        self.b.post("j", me, "mine 1")
        self.assertEqual(self.b.read_new(agent_key="k1"), [])
        self.b.post("j", me, "mine 2")
        o = self.b.post("j", "Other", "theirs").id
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1")], [o])

    def test_peek_does_not_advance(self):
        self.b.allocate_name("k1", "j")
        o = self.b.post("j", "Other", "one").id
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1", advance=False)], [o])
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1", advance=False)], [o])
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1")], [o])
        self.assertEqual(self.b.read_new(agent_key="k1", advance=False), [])

    def test_read_limit_pages(self):
        self.b.allocate_name("k1", "j")
        ids = [self.b.post("j", "Other", f"m{i}").id for i in range(5)]
        with self.h.board(read_limit=2) as b:
            self.assertEqual([m.id for m in b.read_new(agent_key="k1")], ids[:2])
            self.assertEqual([m.id for m in b.read_new(agent_key="k1")], ids[2:4])
            self.assertEqual([m.id for m in b.read_new(agent_key="k1")], ids[4:])
            self.assertEqual(b.read_new(agent_key="k1"), [])

    # ---- catch-up reads (requirement: every message since the last one grabbed)
    def test_read_unread_reports_remaining_and_pages_without_gaps(self):
        self.b.allocate_name("k1", "j")
        ids = [self.b.post("j", "Other", f"m{i}").id for i in range(5)]
        with self.h.board(read_limit=2) as b:
            r = b.read_unread(agent_key="k1")
            self.assertIsInstance(r, ReadResult)
            self.assertEqual(([m.id for m in r.messages], r.remaining), (ids[:2], 3))
            r = b.read_unread(agent_key="k1")
            self.assertEqual(([m.id for m in r.messages], r.remaining), (ids[2:4], 1))
            late = self.b.post("j", "Other", "arrived while paging").id
            r = b.read_unread(agent_key="k1")
            self.assertEqual(([m.id for m in r.messages], r.remaining), ([ids[4], late], 0))
            self.assertEqual(b.read_unread(agent_key="k1"), ReadResult([], 0))

    def test_paging_with_own_posts_interleaved(self):
        me = self.b.allocate_name("k1", "j")
        o1 = self.b.post("j", "Other", "o1").id
        self.b.post("j", me, "mine 1")
        o2 = self.b.post("j", "Other", "o2").id
        self.b.post("j", me, "mine 2")
        o3 = self.b.post("j", "Other", "o3").id
        self.b.post("j", me, "mine 3")
        with self.h.board(read_limit=2) as b:
            r = b.read_unread(agent_key="k1")
            self.assertEqual(([m.id for m in r.messages], r.remaining), ([o1, o2], 1))
            r = b.read_unread(agent_key="k1")
            self.assertEqual(([m.id for m in r.messages], r.remaining), ([o3], 0))
            self.b.post("j", me, "mine 4")
            self.assertEqual(b.read_unread(agent_key="k1"), ReadResult([], 0))
            o4 = self.b.post("j", "Other", "o4").id
            self.assertEqual([m.id for m in b.read_unread(agent_key="k1").messages], [o4])

    def test_peek_reports_remaining_and_changes_nothing(self):
        self.b.allocate_name("k1", "j")
        ids = [self.b.post("j", "Other", f"m{i}").id for i in range(3)]
        with self.h.board(read_limit=1) as b:
            r = b.read_unread(agent_key="k1", advance=False)
            self.assertEqual(([m.id for m in r.messages], r.remaining), (ids[:1], 2))
            self.assertEqual(b.read_unread(agent_key="k1", advance=False), r)

    def test_concurrent_reads_of_one_agent_deliver_each_message_exactly_once(self):
        # Parallel tool calls fire parallel PreToolUse hooks for the same agent.
        self.b.allocate_name("k1", "j")
        total, delivered, errors = 60, [], []
        # Seed a page so concurrent readers have work even before the posters run.
        for i in range(3):
            self.b.post("j", "Other", f"seed{i}")
        start = threading.Barrier(7)

        def reader():
            try:
                with self.h.board(read_limit=7) as b:
                    start.wait()
                    # A finite workload avoids idle readers continually writing last_seen
                    # and starving posters until the database busy timeout.
                    for _ in range(total):
                        delivered.extend(m.id for m in b.read_unread(agent_key="k1").messages)
            except Exception as exc:  # pragma: no cover
                import traceback
                errors.append((exc, traceback.format_exc()))

        def poster():
            try:
                with self.h.board() as b:
                    start.wait()
                    for i in range((total - 3) // 3):
                        b.post("j", "Other", f"p{i}")
            except Exception as exc:  # pragma: no cover
                import traceback
                errors.append((exc, traceback.format_exc()))

        readers = [threading.Thread(target=reader) for _ in range(4)]
        posters = [threading.Thread(target=poster) for _ in range(3)]
        for t in readers + posters:
            t.start()
        try:
            for t in posters:
                t.join(120)
        finally:
            for t in readers:
                t.join(120)
        self.assertFalse(any(t.is_alive() for t in posters + readers),
                         "board workers did not finish before fixture cleanup")
        # drain whatever the last round left (a lost compare-and-set returns nothing)
        with self.h.board(read_limit=1000) as b:
            delivered.extend(m.id for m in b.read_unread(agent_key="k1").messages)
        self.assertEqual(errors, [])
        all_ids = [m.id for m in self.b.messages_after(0, "j")]
        self.assertEqual(len(all_ids), total)
        self.assertEqual(sorted(delivered), all_ids)  # nothing skipped, nothing twice

    def test_new_agent_gets_the_jobs_recent_history_capped(self):
        ids = [self.b.post("j", "Other", f"h{i}").id for i in range(10)]
        self.b.post("elsewhere", "Other", "not this job")
        with self.h.board(join_history=4, read_limit=50) as b:
            b.allocate_name("k1", "j")
            self.assertEqual([m.id for m in b.read_new(agent_key="k1")], ids[-4:])
        with self.h.board(join_history=0) as b:
            b.allocate_name("k2", "j")
            self.assertEqual(b.read_new(agent_key="k2"), [])
            new = self.b.post("j", "Other", "after joining").id
            self.assertEqual([m.id for m in b.read_new(agent_key="k2")], [new])
        with self.h.board(join_history=50) as b:
            b.allocate_name("k3", "j")
            self.assertEqual([m.id for m in b.read_new(agent_key="k3")][:10], ids)

    def test_post_after_a_read_is_never_behind_the_cursor(self):
        # Spooled posts are delivered through post(): their id is assigned at delivery, so
        # it is above every cursor already moved, however old the post is.
        self.b.allocate_name("k1", "j")
        self.b.post("j", "Other", "first")
        self.b.read_new(agent_key="k1")
        late = self.b.post("j", "Other", "written long ago, delivered now").id
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1")], [late])

    # ---- roster and per-agent sync state
    def test_tool_started_returns_the_active_member(self):
        name = self.b.allocate_name("k1", "j")
        self.assertEqual(self.b.tool_started("k1", "Bash"), Member(name, "j", False))
        self.assertIsNone(self.b.tool_started("nobody", "Bash"))
        self.b.allocate_name("k1", "other")  # moved: the member's job is its row's job
        self.assertEqual(self.b.tool_started("k1", "Bash").job, "other")
        self.b.agent_stopped("k1")
        self.assertIsNone(self.b.tool_started("k1", "Bash"))

    def test_tool_started_flags_an_unverified_route(self):
        name = self.b.allocate_name("k1", "j")
        self.b.record_route("k1", "sess", "unverified", "j")
        self.assertEqual(self.b.tool_started("k1", "Bash"), Member(name, "j", True))
        self.b.record_route("k1", "sess", "final", "j")
        self.assertEqual(self.b.tool_started("k1", "Bash"), Member(name, "j", False))

    # ---- routes (which job a subagent belongs to)
    def test_route_of_an_unknown_agent_is_empty(self):
        self.assertEqual(self.b.route("nobody"), Route(None, None, None, None))

    def test_record_route_replaces_and_reports_the_member_job(self):
        self.b.record_route("k1", "sess", "pending")
        self.assertEqual(self.b.route("k1"), Route("pending", None, "sess", None))
        self.b.record_route("k1", "sess", "final", "j")
        self.assertEqual(self.b.route("k1"), Route("final", "j", "sess", None))
        self.b.allocate_name("k1", "j")
        self.assertEqual(self.b.route("k1").member_job, "j")
        self.b.agent_stopped("k1")  # departed members keep their job
        self.assertEqual(self.b.route("k1"), Route("final", "j", "sess", "j"))
        self.b.record_route("k1", "other-sess", "final", None)
        self.assertEqual(self.b.route("k1"), Route("final", None, "other-sess", "j"))
        # a member with no recorded route
        self.b.allocate_name("k2", "j2")
        self.assertEqual(self.b.route("k2"), Route(None, None, None, "j2"))

    def test_claim_route_is_a_compare_and_set(self):
        self.assertTrue(self.b.claim_route("k1", "sess", None, "final", "j"))  # None: no route yet
        self.assertFalse(self.b.claim_route("k1", "sess", None, "final", "other"))
        self.assertEqual(self.b.route("k1"), Route("final", "j", "sess", None))
        self.b.record_route("k2", "sess", "pending")
        self.assertFalse(self.b.claim_route("k2", "sess", "unverified", "final", "j"))
        self.assertTrue(self.b.claim_route("k2", "sess", "pending", "final", None))
        self.assertFalse(self.b.claim_route("k2", "sess", "pending", "final", "j"))
        self.assertEqual(self.b.route("k2"), Route("final", None, "sess", None))
        with self.assertRaises(Exception):
            self.b.claim_route("k2", "sess", "final", "maybe")

    def test_claim_route_has_one_winner_under_concurrency(self):
        self.b.record_route("k", "sess", "pending")
        barrier, wins, errors = threading.Barrier(6, timeout=120), [], []

        def claimer(i):
            try:
                with self.h.board() as b:
                    barrier.wait()
                    wins.append(b.claim_route("k", "sess", "pending", "final", f"j{i}"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=claimer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(wins), [False] * 5 + [True])

    def test_record_route_rejects_unknown_states(self):
        with self.assertRaises(Exception):
            self.b.record_route("k1", "sess", "maybe")

    def test_roster(self):
        a = self.b.allocate_name("a", "j", "worker")
        b = self.b.allocate_name("b", "j")
        self.b.allocate_name("c", "j", "tester")
        self.b.allocate_name("x", "other")
        self.h.backdate_agent("a", joined_at=30)
        self.h.backdate_agent("b", joined_at=20)
        self.h.backdate_agent("c", joined_at=10)
        self.b.tool_started("a", "Bash")
        self.b.agent_stopped("c")
        roster = self.b.roster("j")
        self.assertTrue(all(isinstance(e, RosterEntry) for e in roster))
        self.assertEqual([(e.agent_key, e.role, e.status, e.current_tool, e.active) for e in roster],
                         [("a", "worker", "running", "Bash", True), ("b", None, "started", None, True),
                          ("c", "tester", "completed", None, False)])
        self.assertEqual([e.name for e in roster[:2]], [a, b])
        self.assertEqual(self.b.roster("empty"), [])

    def test_sync_state_defaults_and_records(self):
        name = self.b.allocate_name("k1", "j")
        s = self.b.sync_state("k1")
        self.assertIsInstance(s, SyncState)
        self.assertEqual((s.name, s.roster_seen, s.roster_synced_at, s.memory_recalled_at, s.memory_seen,
                          s.remembered_at, s.nudged_at), (name, None, None, None, (), None, None))
        self.assertIsNotNone(s.joined_at.tzinfo)
        self.assertGreaterEqual(s.now, self.test_started)
        self.assertLessEqual(s.now, self.b.now())
        self.assertIsNone(self.b.sync_state("nobody"))
        self.b.record_roster_sync("k1", "snap-1", full=False)
        s = self.b.sync_state("k1")
        self.assertEqual((s.roster_seen, s.roster_synced_at), ("snap-1", None))
        self.b.record_roster_sync("k1", "snap-2", full=True)
        s = self.b.sync_state("k1")
        self.assertEqual(s.roster_seen, "snap-2")
        self.assertIsNotNone(s.roster_synced_at)
        self.b.record_memory_recall("k1", ["m1", "m2"])
        self.b.record_memory_recall("k1", ["m2", "m3"])
        s = self.b.sync_state("k1")
        self.assertEqual(s.memory_seen, ("m1", "m2", "m3"))
        self.assertIsNotNone(s.memory_recalled_at)
        self.b.record_memory_recall("k1", [])
        self.assertEqual(self.b.sync_state("k1").memory_seen, ("m1", "m2", "m3"))
        self.b.record_memory_recall("k1", [f"x{i}" for i in range(MEMORY_SEEN_MAX + 10)])
        seen = self.b.sync_state("k1").memory_seen
        self.assertEqual(len(seen), MEMORY_SEEN_MAX)
        self.assertEqual(seen[-1], f"x{MEMORY_SEEN_MAX + 9}")
        self.b.record_remembered(name)
        self.b.record_remembered("Nobody At All")  # no error
        self.b.record_nudge("k1")
        s = self.b.sync_state("k1")
        self.assertIsNotNone(s.remembered_at)
        self.assertIsNotNone(s.nudged_at)

    def test_turn_state_is_roster_plus_sync_state(self):
        self.b.allocate_name("k1", "j")
        self.b.allocate_name("k2", "j")
        self.b.record_roster_sync("k1", "snap", full=True)
        roster, state = self.b.turn_state("k1", "j")
        self.assertEqual([e.agent_key for e in roster], [e.agent_key for e in self.b.roster("j")])
        self.assertEqual((state.name, state.roster_seen), (self.b.active_agent_name("k1"), "snap"))
        roster, state = self.b.turn_state("nobody", "j")
        self.assertEqual((len(roster), state), (2, None))

    def test_replies_owed(self):
        me = self.b.allocate_name("k1", "j")
        self.b.allocate_name("k2", "j")
        ask = self.b.post("j", "Asker", "can you check X?", to=me).id
        self.b.post("j", "Asker", "broadcast, not a question to you")
        self.b.post("j", "Other", "for someone else", to="Somebody Else")
        self.assertEqual(self.b.sync_state("k1").replies_owed, ())  # not delivered yet
        self.b.read_new(agent_key="k1")
        owed = self.b.sync_state("k1").replies_owed
        self.assertEqual([(o.id, o.sender) for o in owed], [(ask, "Asker")])
        self.assertIsInstance(owed[0], OwedReply)
        self.assertIsNotNone(owed[0].created_at.tzinfo)
        self.assertEqual(self.b.turn_state("k1", "j")[1].replies_owed, owed)
        self.b.post("j", me, "answer to the wrong person", to="Other")
        self.assertEqual(len(self.b.sync_state("k1").replies_owed), 1)
        self.b.post("j", me, "done, X is fine", to="Asker")
        self.assertEqual(self.b.sync_state("k1").replies_owed, ())
        again = self.b.post("j", "Asker", "and Y?", to=me).id
        self.b.read_new(agent_key="k1")
        self.assertEqual([o.id for o in self.b.sync_state("k1").replies_owed], [again])
        self.b.record_reply_reminder("k1", again)  # reminded once: no longer reported
        s = self.b.sync_state("k1")
        self.assertEqual((s.replies_owed, s.reply_reminded_id), ((), again))
        self.b.record_reply_reminder("k1", again - 1)  # never moves back
        self.assertEqual(self.b.sync_state("k1").reply_reminded_id, again)

    def test_silence_counters(self):
        me = self.b.allocate_name("k1", "j")
        for tool in ("Bash", "Read", "Grep"):
            self.b.tool_started("k1", tool)
        s = self.b.sync_state("k1")
        self.assertEqual((s.tool_calls, s.calls_at_post, s.last_post_at, s.silence_nudged_at), (3, 0, None, None))
        self.b.post("j", me, "status: reading the code")
        self.b.tool_started("k1", "Edit")
        s = self.b.sync_state("k1")
        self.assertEqual((s.tool_calls, s.calls_at_post), (4, 3))
        self.assertIsNotNone(s.last_post_at)
        self.b.record_silence_nudge("k1")
        self.assertIsNotNone(self.b.sync_state("k1").silence_nudged_at)

    def test_fresh_row_resets_sync_state_and_revive_keeps_it(self):
        self.h.reset({"simpsons": ["Homer Simpson"], "english": []})
        self.b.allocate_name("k1", "j")
        self.b.record_memory_recall("k1", ["m1"])
        self.b.record_roster_sync("k1", "snap", full=True)
        self.b.record_silence_nudge("k1")
        self.b.record_reply_reminder("k1", 7)
        self.b.agent_stopped("k1")
        self.b.allocate_name("k1", "j")  # revived, same name
        self.assertEqual(self.b.sync_state("k1").memory_seen, ("m1",))
        self.b.agent_stopped("k1")
        self.b.allocate_name("k2", "j")  # takes Homer
        self.b.allocate_name("k1", "j")  # fresh name, fully reset row
        s = self.b.sync_state("k1")
        self.assertEqual((s.memory_seen, s.roster_seen, s.roster_synced_at, s.silence_nudged_at,
                          s.reply_reminded_id, s.calls_at_post), ((), None, None, None, 0, 0))

    def test_job_project(self):
        self.b.open_job("j", None, None, None, None, project="proj")
        self.assertEqual(self.b.job_status("j").project, "proj")
        self.b.open_job("j", "d", None, None, None)
        self.assertEqual(self.b.job_status("j").project, "proj")
        self.b.ensure_job("plain")
        self.assertIsNone(self.b.job_status("plain").project)
        self.assertEqual({j.job: j.project for j in self.b.jobs()}, {"j": "proj", "plain": None})

    def test_read_other_job_and_unknown_reader(self):
        self.b.allocate_name("k1", "j")
        x = self.b.post("x", "Other", "elsewhere").id
        self.assertEqual([m.id for m in self.b.read_new(agent_key="k1", job="x")], [x])
        self.assertEqual(self.b.read_new(agent_key="nobody"), [])
        self.assertEqual(self.b.read_new(name="Nobody At All"), [])
        self.b.agent_stopped("k1")
        self.assertEqual(self.b.read_new(agent_key="k1"), [])

    def test_read_new_refreshes_last_seen(self):
        self.b.allocate_name("k1", "j")
        self.h.backdate_agent("k1", last_seen=10 * MIN)
        self.assertEqual(self.status_of("k1"), "idle")
        self.b.read_new(agent_key="k1")
        self.assertEqual(self.status_of("k1"), "started")

    def test_recent_and_after(self):
        ids = [self.b.post("j", "A", f"m{i}").id for i in range(4)]
        x = self.b.post("closed", "A", "c").id
        self.b.close_job("closed", "completed", None)
        self.assertEqual([m.id for m in self.b.recent_messages(2, "j")], ids[2:])
        self.assertEqual([m.id for m in self.b.recent_messages(10)], ids + [x])
        self.assertEqual([m.id for m in self.b.recent_messages(10, active_jobs_only=True)], ids)
        self.assertEqual([m.id for m in self.b.messages_after(ids[1])], ids[2:] + [x])
        self.assertEqual([m.id for m in self.b.messages_after(ids[1], "j")], ids[2:])

    # ---- retention
    def test_purge(self):
        old = self.b.post("j", "A", "old").id
        new = self.b.post("j", "A", "new").id
        self.h.backdate_message(old, 8 * DAY)
        self.b.allocate_name("stale", "j")
        self.b.allocate_name("gone", "j")
        self.b.allocate_name("fresh", "j")
        self.h.backdate_agent("stale", last_seen=13 * 3600)
        self.b.agent_stopped("gone")
        self.h.backdate_agent("gone", left_at=8 * DAY)
        self.b.ensure_job("oldjob")
        self.h.backdate_job("oldjob", created_at=8 * DAY)
        self.b.ensure_job("oldbusy")
        self.b.post("oldbusy", "A", "keeps it")
        self.h.backdate_job("oldbusy", created_at=8 * DAY)
        self.b.record_route("old-route", "s", "final", None)
        self.b.record_route("new-route", "s", "pending")
        self.h.backdate_route("old-route", 8 * DAY)
        self.b.purge()
        self.assertEqual(self.b.route("old-route").state, None)
        self.assertEqual(self.b.route("new-route").state, "pending")
        self.assertEqual([m.id for m in self.b.recent_messages(10, "j")], [new])
        agents = {a.agent_key: a for a in self.b.agents("j")}
        self.assertNotIn("gone", agents)
        self.assertEqual(agents["stale"].status, "dead")
        self.assertIsNotNone(agents["stale"].ended_at)
        self.assertEqual(agents["fresh"].status, "started")
        self.assertIsNone(self.b.job_status("oldjob"))
        self.assertIsNotNone(self.b.job_status("oldbusy"))
        self.assertIsNotNone(self.b.job_status("j"))
        self.b.purge()  # idempotent

    def test_allocate_purges_first(self):
        self.h.reset({"simpsons": ["Homer Simpson"], "english": []})
        self.b.allocate_name("stale", "j")
        self.h.backdate_agent("stale", last_seen=13 * 3600)
        self.assertEqual(self.b.allocate_name("k2", "j"), "Homer Simpson")

    # ---- jobs
    def test_ensure_job(self):
        self.b.ensure_job("j", "desc", "me")
        self.b.ensure_job("j")
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.description, s.created_by, s.activated_at), ("active", "desc", "me", None))
        self.b.ensure_job("j", "new desc", "someone else")
        s = self.b.job_status("j")
        self.assertEqual((s.description, s.created_by), ("new desc", "me"))

    def test_open_and_close_job(self):
        self.b.open_job("j", "d", "the task", None, "me")
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.description, s.task, s.session_id, s.created_by), ("active", "d", "the task", None, "me"))
        self.assertIsNotNone(s.activated_at)
        self.assertFalse(self.b.close_job("nope", "completed", "x"))
        self.assertTrue(self.b.close_job("j", "failed", "it broke"))
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome), ("failed", "it broke"))
        self.assertIsNotNone(s.finished_at)
        self.assertTrue(self.b.close_job("j", "cancelled", None))
        self.assertEqual(self.b.job_status("j").outcome, "it broke")
        self.b.open_job("j", None, None, "sess", "other")
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome, s.finished_at, s.description, s.task, s.session_id, s.created_by),
                         ("active", None, None, "d", "the task", "sess", "me"))

    def test_bind_job_session_first_wins(self):
        self.b.bind_job_session("missing", "s0")  # no-op, no error
        self.assertIsNone(self.b.job_status("missing"))
        self.b.open_job("j", None, None, None, None)
        self.b.bind_job_session("j", "s1")
        self.b.bind_job_session("j", "s2")
        self.assertEqual(self.b.job_status("j").session_id, "s1")

    def test_close_job_marks_active_agents_left(self):
        self.b.open_job("j", None, None, None, None)
        self.b.allocate_name("a", "j")
        self.b.allocate_name("b", "j")
        self.b.allocate_name("c", "j")
        self.b.allocate_name("elsewhere", "other")
        self.b.tool_started("a", "Bash")
        self.b.agent_stopped("c")
        self.b.close_job("j", "completed", "ok")
        st = {a.agent_key: a for a in self.b.agents("j")}
        self.assertEqual({k: v.status for k, v in st.items()}, {"a": "left", "b": "left", "c": "completed"})
        self.assertIsNone(st["a"].current_tool)
        self.assertEqual(self.b.agents("j", include_departed=False), [])
        self.assertEqual(self.status_of("elsewhere", "other"), "started")

    def test_session_jobs_is_the_sessions_jobs_closed_ones_included_in_jobs_order(self):
        self.b.open_job("a", "A", None, "S1", None)
        self.b.open_job("b", "B", None, "S2", None)
        self.b.open_job("c", "C", None, "S1", None)
        self.b.open_job("d", "D", None, None, None)
        self.b.post("a", "X", "m1")
        self.b.close_job("c", "completed", None)
        mine = self.b.session_jobs("S1")
        self.assertEqual([j.job for j in mine], ["a", "c"])
        self.assertEqual(mine, [j for j in self.b.jobs(True) if j.session_id == "S1"])   # same rollups
        self.assertEqual(mine[0].messages, 1)
        self.assertEqual(self.b.session_jobs("nobody"), [])

    def test_session_jobs_does_not_load_unrelated_job_rollups(self):
        self.b.open_job("mine", None, None, "S1", None)
        self.b.open_job("old", None, None, "S1", None)
        self.b.open_job("other", None, None, "S2", None)
        self.b.close_job("old", "completed", None)
        self.b.post("mine", "X", "local")
        self.b.post("old", "X", "history")
        self.b.post("other", "X", "unrelated")
        expected = [self.b.job_status(j) for j in ("mine", "old")]
        with mock.patch.object(self.b, "jobs", side_effect=AssertionError("whole-board rollups")):
            self.assertEqual(self.b.session_jobs("S1"), expected)
            self.assertEqual(self.b.session_jobs("missing"), [])

    def test_session_shown_jobs_excludes_history_and_keeps_last_finished(self):
        from swarm import cli
        for job in ("old-a", "old-b", "live-a", "live-b"):
            self.b.open_job(job, None, None, "S1", None)
        self.b.open_job("other", None, None, "S2", None)
        self.b.close_job("old-a", "completed", None)
        self.b.close_job("old-b", "completed", None)
        for expected in (["live-a", "live-b"], ["live-b"], ["live-b"]):
            ever = self.b.session_jobs("S1")
            active = [j for j in ever if j.status == "active"]
            shown = active or [max(ever, key=lambda j: (j.finished_at or j.created_at, j.job))]
            with mock.patch.object(self.b, "session_jobs", side_effect=AssertionError("history rollups")):
                rows = self.b.session_shown_jobs("S1")
                self.assertEqual([j.job for j in rows], expected)
                self.assertEqual(rows, shown)
                self.assertEqual(cli.session_jobs(self.b, "S1")[0], shown)
                self.assertEqual(self.b.session_shown_jobs("missing"), [])
            if expected == ["live-a", "live-b"]:
                self.b.close_job("live-a", "completed", None)
            elif rows[0].status == "active":
                self.b.close_job("live-b", "completed", None)

    def test_jobs_listing_and_rollup(self):
        self.b.open_job("a", "A", None, None, None)
        self.b.open_job("b", "B", None, None, None)
        self.b.open_job("c", "C", None, None, None)
        for k in ("s", "r", "i", "done", "left"):
            self.b.allocate_name(k, "a")
        self.b.tool_started("r", "Read")
        self.h.backdate_agent("i", last_seen=6 * MIN)
        self.b.agent_stopped("done")
        self.b.leave(agent_key="left")
        self.b.post("a", "X", "m1")
        self.b.post("a", "X", "m2")
        self.b.close_job("c", "completed", None)
        s = self.b.job_status("a")
        self.assertIsInstance(s, JobStatus)
        self.assertEqual((s.agents, s.started, s.running, s.idle, s.completed, s.dead_or_left, s.messages),
                         (5, 1, 1, 1, 1, 1, 2))
        self.assertIsNotNone(s.last_activity_at)
        empty = self.b.job_status("b")
        self.assertEqual((empty.agents, empty.messages, empty.last_activity_at), (0, 0, None))
        self.assertIsNone(self.b.job_status("nope"))
        self.assertEqual([j.job for j in self.b.jobs()], ["a", "b"])
        self.assertEqual([j.job for j in self.b.jobs(include_closed=True)], ["a", "b", "c"])
        # the order is stable: activity must not reshuffle the table every refresh
        self.b.post("b", "X", "newest")
        self.assertEqual([j.job for j in self.b.jobs()], ["a", "b"])

    # ---- agents and derived status
    def test_status_transitions(self):
        self.b.allocate_name("k", "j")
        self.assertEqual(self.status_of("k"), "started")
        self.b.tool_started("k", "Bash")
        a = self.agent("k")
        self.assertEqual((a.status, a.current_tool, a.tool_calls), ("running", "Bash", 1))
        self.b.tool_finished("k")
        a = self.agent("k")
        self.assertEqual((a.status, a.current_tool), ("running", None))
        self.h.backdate_agent("k", last_seen=6 * MIN)
        self.assertEqual(self.status_of("k"), "idle")
        self.h.backdate_agent("k", last_seen=31 * MIN)
        self.assertEqual(self.status_of("k"), "dead")
        self.b.tool_started("k", "Read")
        self.assertEqual(self.agent("k").tool_calls, 2)
        self.h.backdate_agent("k", last_seen=45 * MIN, tool_started_at=45 * MIN)
        self.assertEqual(self.status_of("k"), "running")  # long tool call in flight
        self.h.backdate_agent("k", last_seen=61 * MIN, tool_started_at=61 * MIN)
        self.assertEqual(self.status_of("k"), "dead")  # tool timeout exceeded
        self.b.agent_stopped("k")
        a = self.agent("k")
        self.assertEqual((a.status, a.current_tool), ("completed", None))
        self.assertIsNotNone(a.ended_at)

    def test_idle_started_agent(self):
        self.b.allocate_name("k", "j")
        self.h.backdate_agent("k", last_seen=6 * MIN)
        self.assertEqual(self.status_of("k"), "idle")
        self.b.tool_finished("k")  # PostToolUse contact revives it
        self.assertEqual(self.status_of("k"), "started")

    def test_tool_name_truncated_and_default(self):
        self.b.allocate_name("k", "j")
        self.b.tool_started("k", "T" * 200)
        self.assertEqual(self.agent("k").current_tool, "T" * 80)
        self.b.tool_started("k", None)
        self.assertEqual(self.agent("k").current_tool, "?")

    def test_bookkeeping_ignores_departed_agents(self):
        self.b.allocate_name("k", "j")
        self.b.agent_stopped("k")
        self.b.tool_started("k", "Bash")
        self.b.tool_finished("k")
        self.b.agent_stopped("k")
        a = self.agent("k")
        self.assertEqual((a.status, a.tool_calls, a.current_tool), ("completed", 0, None))
        self.b.tool_started("nobody", "Bash")  # unknown key: no error

    def test_leave(self):
        n = self.b.allocate_name("k1", "j")
        self.b.allocate_name("k2", "j")
        self.b.tool_started("k1", "Bash")
        self.assertTrue(self.b.leave(name=n))
        self.assertFalse(self.b.leave(name=n))
        a = self.agent("k1")
        self.assertEqual((a.status, a.current_tool), ("left", None))
        self.assertTrue(self.b.leave(agent_key="k2"))
        self.assertFalse(self.b.leave(agent_key="k2"))
        self.assertFalse(self.b.leave(agent_key="nobody"))

    def test_agents_order_and_filter(self):
        self.b.allocate_name("first", "j")
        self.b.allocate_name("second", "j")
        self.b.allocate_name("third", "j")
        self.h.backdate_agent("first", joined_at=30)
        self.h.backdate_agent("second", joined_at=20)
        self.h.backdate_agent("third", joined_at=10)
        self.b.agent_stopped("first")
        self.assertEqual([a.agent_key for a in self.b.agents("j")], ["second", "third", "first"])
        self.assertEqual([a.agent_key for a in self.b.agents("j", include_departed=False)], ["second", "third"])
        a = self.b.agents("j")[0]
        self.assertTrue(a.host)
        self.assertIsNotNone(a.joined_at.tzinfo)

    def test_agent_messages_count_since_joined(self):
        n = self.b.allocate_name("k", "j")
        before = self.b.post("j", n, "counted")
        self.b.post("other", n, "other job: not counted")
        self.assertEqual(self.agent("k").messages, 1)
        self.h.backdate_message(before.id, 3600)  # before it joined: not counted
        self.assertEqual(self.agent("k").messages, 0)

    def test_agent_message_counts_preserve_reused_name_incarnations(self):
        self.h.reset({"simpsons": ["A"]})
        name = self.b.allocate_name("old", "j")
        self.h.backdate_agent("old", joined_at=3600)
        old_post = self.b.post("j", name, "old incarnation")
        self.h.backdate_message(old_post.id, 120)
        self.b.agent_stopped("old")
        self.assertEqual(self.b.allocate_name("new", "j"), name)
        self.h.backdate_agent("new", joined_at=60)
        self.b.post("j", name, "new incarnation")
        self.b.post("other", name, "same name, other job")
        self.assertEqual(self.agent("old").messages, 2)
        self.assertEqual(self.agent("new").messages, 1)
        self.assertEqual(self.b.job_status("j").messages, 2)

    def test_closed_job_rollups_remain_live_and_match_agent_status(self):
        self.b.open_job("j", "closed", None, None, None)
        self.b.open_job("other", None, None, None, None)
        for key in ("done", "left", "quiet"):
            self.b.allocate_name(key, "j")
        self.b.agent_stopped("done")
        self.b.close_job("j", "completed", "kept")
        for key in ("done", "left", "quiet"):
            self.h.backdate_agent(key, last_seen=DAY)
        self.b.post("j", "X", "post after closure")
        self.b.allocate_name("other-agent", "other")
        self.b.post("other", "X", "unrelated")
        js = self.b.job_status("j")
        self.assertEqual((js.agents, js.started, js.running, js.idle,
                          js.completed, js.dead_or_left, js.messages),
                         (3, 0, 0, 0, 1, 2, 1))
        self.assertEqual(js.last_activity_at, self.b.recent_messages(1, "j")[0].created_at)
        self.assertEqual(js.status, "completed")
        self.assertEqual(js.outcome, "kept")
        self.assertEqual(next(row for row in self.b.jobs(True) if row.job == "j"), js)
        self.assertEqual([row.job for row in self.b.jobs()], ["other"])

    def test_agent_events(self):
        since = self.b.now()
        self.b.allocate_name("a", "j", "worker")
        self.b.allocate_name("b", "other")
        self.b.agent_stopped("a")
        ev = self.b.agent_events(since, "j")
        self.assertEqual(len(ev), 1)
        self.assertIsInstance(ev[0], AgentEvent)
        self.assertEqual((ev[0].job, ev[0].role, ev[0].state), ("j", "worker", "completed"))
        self.assertIsNotNone(ev[0].left_at)
        self.assertEqual({e.job for e in self.b.agent_events(since)}, {"j", "other"})
        self.assertEqual(self.b.agent_events(self.b.now() + dt.timedelta(seconds=5)), [])

    # ---- change notification
    def test_wait_for_change_after_post(self):
        with self.h.board() as watcher:
            watcher.subscribe(messages_only=True)
            self.assertFalse(watcher.wait_for_change(0.2))
            t = threading.Thread(target=lambda: self.b.post("j", "A", "wake up"))
            t.start()
            try:
                self.assertTrue(watcher.wait_for_change(30))
            finally:
                t.join(120)
                self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")

    def test_wait_for_change_drains_burst(self):
        with self.h.board() as watcher:
            watcher.subscribe(messages_only=True)
            for i in range(3):
                self.b.post("j", "A", f"burst {i}")
            self.assertTrue(watcher.wait_for_change(5))
            self.assertFalse(watcher.wait_for_change(0.3))

    def test_wait_for_change_state(self):
        self.b.allocate_name("k", "j")
        with self.h.board() as watcher:
            watcher.subscribe()
            self.b.tool_started("k", "Bash")
            self.assertTrue(watcher.wait_for_change(5))

    # ---- goals, the judge and its verdicts
    def test_goal_is_stored_and_reopening_resets_the_verdict(self):
        self.b.open_job("j", "d", None, None, "me", goal="all tests green")
        s = self.b.job_status("j")
        self.assertEqual((s.goal, s.verdict, s.verdict_reason, s.verdict_by, s.verdict_at, s.judge,
                          s.completion_forced), ("all tests green", None, None, None, None, None, False))
        self.b.allocate_name("jk", "j")
        self.assertTrue(self.b.claim_judge("jk", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("jk"), "met", "ok"))
        self.b.close_job("j", "completed", None, forced=False)
        self.b.open_job("j", None, None, None, "me")  # re-opened: goal kept, verdict cleared
        s = self.b.job_status("j")
        self.assertEqual((s.goal, s.verdict, s.verdict_reason, s.verdict_by, s.verdict_at),
                         ("all tests green", None, None, None, None))
        self.b.open_job("j", None, None, None, "me", goal="new goal")
        self.assertEqual(self.b.job_status("j").goal, "new goal")
        self.b.open_job("nogoal", None, None, None, "me")
        self.assertIsNone(self.b.job_status("nogoal").goal)

    def test_one_active_judge_per_job(self):
        self.b.open_job("j", None, None, None, "me", goal="g")
        first = self.b.allocate_name("a", "j", "general-purpose")
        self.b.allocate_name("b", "j")
        self.b.allocate_name("c", "other")
        self.assertTrue(self.b.claim_judge("a", "j"))
        self.assertTrue(self.b.claim_judge("a", "j"))  # idempotent
        self.assertFalse(self.b.claim_judge("b", "j"))
        self.assertTrue(self.b.claim_judge("c", "other"))  # per job
        self.assertFalse(self.b.claim_judge("nobody", "j"))
        self.assertEqual(self.b.job_status("j").judge, first)
        self.assertEqual({e.agent_key: e.role for e in self.b.roster("j")}, {"a": "judge", "b": None})
        self.b.agent_stopped("a")
        self.assertIsNone(self.b.job_status("j").judge)
        self.assertTrue(self.b.claim_judge("b", "j"))  # the seat is free again
        # the departed judge resumed: back as a plain member, the seat stays with b
        self.assertEqual(self.b.allocate_name("a", "j"), first)
        self.assertFalse(self.b.claim_judge("a", "j"))
        self.assertEqual(self.b.job_status("j").judge, self.b.active_agent_name("b"))

    def test_concurrent_judge_claims_have_one_winner(self):
        self.b.open_job("j", None, None, None, "me", goal="g")
        keys = [f"k{i}" for i in range(5)]
        for k in keys:
            self.b.allocate_name(k, "j")
        barrier, wins, errors = threading.Barrier(5, timeout=120), [], []

        def claimer(k):
            try:
                with self.h.board() as b:
                    barrier.wait()
                    wins.append(b.claim_judge(k, "j"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=claimer, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(wins), [False] * 4 + [True])

    def test_verdict_next_is_stored_replaced_and_cleared_on_reopen(self):
        self.b.open_job("j", None, None, None, "me", goal="G")
        judge = self.b.allocate_name("jk", "j")
        self.b.claim_judge("jk", "j")
        self.assertTrue(self.b.record_verdict("j", judge, "not_met", "why", "change x in y"))
        s = self.b.job_status("j")
        self.assertEqual((s.verdict_reason, s.verdict_next), ("why", "change x in y"))
        self.assertTrue(self.b.record_verdict("j", judge, "met", "fixed"))     # next_steps defaults to None
        self.assertIsNone(self.b.job_status("j").verdict_next)
        self.assertTrue(self.b.record_verdict("j", judge, "not_met", "again", "z"))
        self.b.open_job("j", None, None, None, "me")
        self.assertIsNone(self.b.job_status("j").verdict_next)

    def test_verdict_only_from_the_active_judge(self):
        self.b.open_job("j", None, None, None, "me", goal="g")
        judge = self.b.allocate_name("jk", "j")
        worker = self.b.allocate_name("wk", "j")
        self.b.claim_judge("jk", "j")
        self.assertFalse(self.b.record_verdict("j", worker, "met", "trust me"))
        self.assertFalse(self.b.record_verdict("j", "Nobody", "met", "x"))
        self.assertFalse(self.b.record_verdict("other", judge, "met", "x"))
        self.assertIsNone(self.b.job_status("j").verdict)
        self.assertTrue(self.b.record_verdict("j", judge, "not_met", "no failover test"))
        s = self.b.job_status("j")
        self.assertEqual((s.verdict, s.verdict_reason, s.verdict_by), ("not_met", "no failover test", judge))
        self.assertGreaterEqual(s.verdict_at, self.test_started)
        self.assertLessEqual(s.verdict_at, self.b.now())
        self.assertTrue(self.b.record_verdict("j", judge, "met", "failover tested"))  # re-judged
        self.assertEqual(self.b.job_status("j").verdict, "met")
        with self.assertRaises(Exception):
            self.b.record_verdict("j", judge, "maybe", "x")
        self.b.agent_stopped("jk")
        self.assertFalse(self.b.record_verdict("j", judge, "not_met", "departed judges don't judge"))

    def test_verifiers_many_per_job_and_their_counts(self):
        self.b.open_job("j", None, None, None, "me", goal="g")
        v1 = self.b.allocate_name("v1", "j", "general-purpose")
        v2 = self.b.allocate_name("v2", "j")
        w = self.b.allocate_name("w", "j")
        self.b.allocate_name("jk", "j")
        self.assertTrue(self.b.claim_verifier("v1", "j"))
        self.assertTrue(self.b.claim_verifier("v2", "j"))
        self.assertFalse(self.b.claim_verifier("v1", "other"))
        self.assertFalse(self.b.claim_verifier("nobody", "j"))
        self.b.claim_judge("jk", "j")
        self.assertFalse(self.b.claim_verifier("jk", "j"))   # the judge stays the judge
        roles = {a.agent_key: a.role for a in self.b.agents("j")}
        self.assertEqual((roles["v1"], roles["v2"], roles["w"], roles["jk"]),
                         ("verifier", "verifier", None, "judge"))
        self.b.post("j", v1, "VERIFIED: a")
        self.b.post("j", v2, "FAILED: b: evidence")
        self.b.post("j", v2, "not a result")
        self.b.post("j", w, "VERIFIED: self-checks don't count")
        self.assertEqual(self.b.verification_counts("j"), (1, 1))
        self.assertEqual(self.b.verification_counts("other"), (0, 0))
        self.assertTrue(self.b.tool_started("v1", "Bash").verifier)
        self.assertFalse(self.b.tool_started("w", "Bash").verifier)
        self.b.agent_stopped("v1")   # a departed verifier's results still count
        self.assertEqual(self.b.verification_counts("j"), (1, 1))

    def test_waiting_is_set_cleared_and_reset_by_open_and_close(self):
        self.b.open_job("j", None, None, None, "me")
        s = self.b.job_status("j")
        self.assertEqual((s.waiting_on, s.waiting_since), (None, None))
        self.assertTrue(self.b.set_waiting("j", "the user's answers"))
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.waiting_on), ("active", "the user's answers"))
        self.assertGreaterEqual(s.waiting_since, self.test_started)
        self.assertLessEqual(s.waiting_since, self.b.now())
        self.assertTrue(self.b.set_waiting("j", None))
        s = self.b.job_status("j")
        self.assertEqual((s.waiting_on, s.waiting_since), (None, None))
        self.assertFalse(self.b.set_waiting("nope", "x"))
        self.b.set_waiting("j", "x")
        self.b.open_job("j", None, None, None, "me")   # re-activation: working again
        self.assertIsNone(self.b.job_status("j").waiting_on)
        self.b.set_waiting("j", "x")
        self.b.close_job("j", "completed", None)
        self.assertIsNone(self.b.job_status("j").waiting_on)
        self.assertFalse(self.b.set_waiting("j", "closed jobs don't wait"))
        self.assertIn("j", [x.job for x in self.b.jobs(include_closed=True)])

    def test_spawns_are_capped_per_agent_and_per_job(self):
        self.b.open_job("j", None, None, None, "me")
        for k in ("a", "b", "c"):
            self.b.allocate_name(k, "j")
        self.b.allocate_name("x", "other")
        g = self.b.reserve_spawn("a", "j", 2, 3)
        self.assertEqual((g.granted, g.agent_spawns, g.job_spawns, g.refused), (True, 1, 1, None))
        self.assertTrue(self.b.reserve_spawn("a", "j", 2, 3).granted)
        g = self.b.reserve_spawn("a", "j", 2, 3)
        self.assertEqual((g.granted, g.agent_spawns, g.job_spawns, g.refused), (False, 2, 2, "agent"))
        self.assertTrue(self.b.reserve_spawn("b", "j", 2, 3).granted)
        g = self.b.reserve_spawn("c", "j", 2, 3)
        self.assertEqual((g.granted, g.agent_spawns, g.job_spawns, g.refused), (False, 0, 3, "job"))
        self.assertTrue(self.b.reserve_spawn("x", "other", 2, 3).granted)   # per job
        self.assertEqual(self.b.reserve_spawn("x", "j", 2, 3).refused, "member")
        self.assertEqual(self.b.reserve_spawn("nobody", "j", 2, 3).refused, "member")
        self.b.agent_stopped("b")   # a departed agent's spawns still count for the job
        self.assertEqual(self.b.reserve_spawn("c", "j", 2, 3).refused, "job")
        self.b.open_job("j", None, None, None, "me")   # a new run of the job starts from zero
        self.assertTrue(self.b.reserve_spawn("c", "j", 2, 3).granted)

    def test_concurrent_spawns_never_exceed_the_job_cap(self):
        self.b.open_job("j", None, None, None, "me")
        keys = [f"k{i}" for i in range(6)]
        for k in keys:
            self.b.allocate_name(k, "j")
        barrier, grants, errors = threading.Barrier(6, timeout=120), [], []

        def spawner(k):
            try:
                with self.h.board() as b:
                    barrier.wait()
                    grants.append(b.reserve_spawn(k, "j", 5, 2).granted)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=spawner, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(grants), [False] * 4 + [True] * 2)

    def test_forced_completion_is_recorded(self):
        self.b.open_job("j", None, None, None, "me", goal="g")
        self.assertTrue(self.b.close_job("j", "completed", "shipped anyway", forced=True))
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.completion_forced), ("completed", True))
        self.b.open_job("j", None, None, None, "me")
        self.assertFalse(self.b.job_status("j").completion_forced)

    def test_closed_board_unusable(self):
        b = self.h.board()
        b.close()
        with self.assertRaises(Exception):
            b.ensure_job("j")

    # ---- transcripts
    def trow(self, job="j", key="k1", name="Homer Simpson", text="{\"a\": 1}\n", final=True,
             role="subagent", days_ago=None) -> TranscriptRow:
        import hashlib
        import lzma
        data = text.encode()
        return TranscriptRow(job=job, agent_key=key, agent_name=name, role=role, host="h1",
                             session_id="s1", final=final, raw_bytes=len(data), redactions=2,
                             sha256=hashlib.sha256(data).hexdigest(), body=lzma.compress(data),
                             captured_at=None if days_ago is None else
                             dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago))

    def test_transcript_save_list_and_body(self):
        self.assertEqual(self.b.transcripts(), [])
        self.assertIsNone(self.b.transcript_body("j", "k1"))
        row = self.trow(text='{"x": "hello"}\n')
        self.assertTrue(self.b.save_transcript(row))
        [s] = self.b.transcripts()
        self.assertIsInstance(s, TranscriptSummary)
        self.assertEqual((s.job, s.agent_key, s.agent_name, s.role, s.host, s.session_id, s.final,
                          s.raw_bytes, s.stored_bytes, s.redactions, s.sha256),
                         ("j", "k1", "Homer Simpson", "subagent", "h1", "s1", True, row.raw_bytes,
                          len(row.body), 2, row.sha256))
        self.assertGreaterEqual(s.captured_at, self.test_started)
        self.assertLessEqual(s.captured_at, self.b.now())
        self.assertIsNotNone(s.captured_at.tzinfo)
        self.assertEqual(self.b.transcript_body("j", "k1"), b'{"x": "hello"}\n')

    def test_transcript_body_refuses_a_corrupt_stored_body(self):
        from swarm.board import BoardError
        self.b.save_transcript(self.trow())
        self.h.plant_transcript_body("j", "k1", b"not lzma at all")
        with self.assertRaises(BoardError):
            self.b.transcript_body("j", "k1")

    def test_transcript_body_refuses_a_decompression_bomb(self):
        from support import lzma_bomb
        from swarm.board import BoardError
        from swarm.board.base import TRANSCRIPT_MAX_RAW
        self.b.save_transcript(self.trow())
        self.h.plant_transcript_body("j", "k1", lzma_bomb(TRANSCRIPT_MAX_RAW + 1))
        with self.assertRaises(BoardError) as cm:
            self.b.transcript_body("j", "k1")
        self.assertIn("transcript", str(cm.exception))

    def test_transcript_body_reads_one_at_the_cap(self):
        from support import lzma_bomb
        from swarm.board.base import TRANSCRIPT_MAX_RAW
        self.b.save_transcript(self.trow())
        self.h.plant_transcript_body("j", "k1", lzma_bomb(TRANSCRIPT_MAX_RAW))
        self.assertEqual(len(self.b.transcript_body("j", "k1")), TRANSCRIPT_MAX_RAW)

    def test_memory_ref_excerpt_refuses_a_planted_bomb_and_a_corrupt_blob(self):
        from support import lzma_bomb
        from swarm.board import BoardError
        from swarm.board.base import EXCERPT_MAX_RAW
        self.b.save_memory_ref(self.mref())
        for blob in (lzma_bomb(EXCERPT_MAX_RAW + 1), b"garbage"):
            self.h.plant_memory_excerpt("doc-1", blob)
            with self.assertRaises(BoardError):
                self.b.memory_ref_excerpt("doc-1")

    def test_transcript_unchanged_sha_is_skipped_unless_it_becomes_final(self):
        self.assertTrue(self.b.save_transcript(self.trow(final=False)))
        self.assertFalse(self.b.save_transcript(self.trow(final=False)))
        self.assertTrue(self.b.save_transcript(self.trow(final=True)))
        self.assertFalse(self.b.save_transcript(self.trow(final=False)))   # never demoted for nothing
        self.assertTrue(self.b.transcripts()[0].final)
        self.assertTrue(self.b.save_transcript(self.trow(text="{}\n", final=False)))
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.raw_bytes), (False, 3))
        self.assertEqual(self.b.transcript_body("j", "k1"), b"{}\n")

    # ---- capture-failed rows (schema 9)
    def failed_row(self, job="j", key="k1", reason="ran out of time"):
        from swarm.transcripts import capture_failed_row
        return capture_failed_row(job, key, "Homer Simpson", reason, host="h1", session_id="s1",
                                  harness="claude")

    def test_capture_failed_marker_alone_is_final_bodiless_and_listed(self):
        from swarm.board.base import bodiless
        self.assertEqual(self.b.mark_capture_failed("j", "k1", "ran out of time", self.failed_row()), "stored")
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.failed, s.raw_bytes, s.redactions, s.images),
                         (True, "ran out of time", 0, 0, ()))
        self.assertTrue(bodiless(s))
        self.assertIsNone(self.b.transcript_body("j", "k1"))   # readers see no body at all
        self.b.save_transcript(self.trow(key="k2"))
        self.assertIsNone({r.agent_key: r for r in self.b.transcripts()}["k2"].failed)

    def test_capture_failed_never_touches_a_final_capture(self):
        self.assertTrue(self.b.save_transcript(self.trow(text='{"x": 1}\n')))
        self.assertEqual(self.b.mark_capture_failed("j", "k1", "ran out of time", self.failed_row()), "kept")
        self.assertFalse(self.b.save_transcript(self.failed_row()))   # nor a marker saved directly
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.failed), (True, None))
        self.assertEqual(self.b.transcript_body("j", "k1"), b'{"x": 1}\n')

    def test_capture_failed_over_a_snapshot_keeps_its_body_images_and_sha(self):
        import hashlib
        import lzma
        from support import fake_image
        from swarm.board import TranscriptImage
        from swarm.board.base import bodiless
        data = fake_image(7)
        img = TranscriptImage(hashlib.sha256(data).hexdigest(), "image/png", len(data), data)
        text = b'{"snap": 1}\n'
        snap = TranscriptRow(job="j", agent_key="k1", agent_name="Homer Simpson", role="subagent", host="h1",
                             session_id="s1", final=False, raw_bytes=len(text), redactions=2,
                             sha256=hashlib.sha256(text).hexdigest(), body=lzma.compress(text), images=(img,))
        self.assertTrue(self.b.save_transcript(snap))
        [before] = self.b.transcripts()
        self.assertEqual(self.b.mark_capture_failed("j", "k1", "ran out of time", self.failed_row()), "marked")
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.failed), (True, "ran out of time"))
        self.assertEqual((s.sha256, s.raw_bytes, s.stored_bytes, s.redactions, s.images, s.captured_at),
                         (before.sha256, before.raw_bytes, before.stored_bytes, 2, before.images, before.captured_at))
        self.assertFalse(bodiless(s))
        self.assertEqual(self.b.transcript_body("j", "k1"), text)          # the snapshot is still read
        self.assertEqual(self.b.transcript_image(img.sha256).data, data)
        # a second give-up keeps it as it is
        self.assertEqual(self.b.mark_capture_failed("j", "k1", "again", self.failed_row()), "kept")
        self.assertEqual(self.b.transcripts()[0].failed, "ran out of time")
        # a later real capture clears the mark
        self.assertTrue(self.b.save_transcript(self.trow(text='{"late": 1}\n')))
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.failed), (True, None))
        self.assertEqual(self.b.transcript_body("j", "k1"), b'{"late": 1}\n')

    def test_an_unchanged_final_capture_clears_the_mark(self):
        row = self.trow(final=False)
        self.b.save_transcript(row)
        self.b.mark_capture_failed("j", "k1", "ran out of time", self.failed_row())
        self.assertTrue(self.b.refresh_transcript("j", "k1", True))   # the same content, captured final
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.failed, s.sha256), (True, None, row.sha256))

    def test_a_later_capture_replaces_a_bodiless_marker(self):
        self.b.mark_capture_failed("j", "k1", "ran out of time", self.failed_row())
        self.assertTrue(self.b.save_transcript(self.trow(text='{"late": 1}\n')))
        [s] = self.b.transcripts()
        self.assertEqual((s.final, s.failed), (True, None))
        self.assertEqual(self.b.transcript_body("j", "k1"), b'{"late": 1}\n')

    def test_capture_failed_ends_the_pending_final(self):
        import getpass
        me, host = getpass.getuser(), compat.node()
        self.b.allocate_name("k1", "j")
        self.b.set_agent_runtime("k1", "claude", "m")
        self.b.agent_stopped("k1")
        since = self.b.now() - dt.timedelta(days=1)
        self.assertEqual(self.b.pending_final_transcripts(host, me, "claude", since), [("j", "k1")])
        self.b.mark_capture_failed("j", "k1", "ran out of time", self.failed_row())
        self.assertEqual(self.b.pending_final_transcripts(host, me, "claude", since), [])

    def test_transcript_filters(self):
        self.b.save_transcript(self.trow("j", "k1", "Homer Simpson"))
        self.b.save_transcript(self.trow("j", "k2", "Bart Simpson"))
        self.b.save_transcript(self.trow("j2", "k3", "Homer Simpson"))
        self.b.save_transcript(self.trow("j", "orchestrator", "orchestrator", role="orchestrator"))
        keys = lambda **kw: sorted((s.job, s.agent_key) for s in self.b.transcripts(**kw))  # noqa: E731
        self.assertEqual(len(self.b.transcripts()), 4)
        self.assertEqual(keys(job="j2"), [("j2", "k3")])
        self.assertEqual(keys(agent_name="Homer Simpson"), [("j", "k1"), ("j2", "k3")])
        self.assertEqual(keys(agent_key="k2"), [("j", "k2")])
        self.assertEqual(keys(role="orchestrator"), [("j", "orchestrator")])
        self.assertEqual(keys(job="j", agent_name="Homer Simpson"), [("j", "k1")])
        self.assertIsNone(self.b.transcript_body("j2", "k1"))

    def test_transcripts_ordered_by_capture_time(self):
        self.b.save_transcript(self.trow("j", "k1", days_ago=1))
        self.b.save_transcript(self.trow("j", "k2", days_ago=3))
        self.b.save_transcript(self.trow("j", "k3", days_ago=2))
        self.assertEqual([s.agent_key for s in self.b.transcripts()], ["k2", "k3", "k1"])

    def test_transcript_totals(self):
        self.assertEqual(self.b.transcript_totals(), (0, 0, 0, None, 0, 0))
        r1, r2 = self.trow("j", "k1", days_ago=3), self.trow("j2", "k2", text="{}\n")
        self.b.save_transcript(r1)
        self.b.save_transcript(r2)
        stored, raw, jobs, oldest, _, _ = self.b.transcript_totals()
        self.assertEqual((stored, raw, jobs), (len(r1.body) + len(r2.body), r1.raw_bytes + r2.raw_bytes, 2))
        self.assertEqual(oldest, r1.captured_at)

    def test_transcript_rotation_by_time_spares_active_jobs(self):
        self.b.save_transcript(self.trow("old", "k1", days_ago=40))
        self.b.save_transcript(self.trow("act", "k2", days_ago=40))
        self.b.save_transcript(self.trow("new", "k3", days_ago=1))
        self.assertEqual(self.b.rotate_transcripts(30, 0, ["act"]), 1)
        self.assertEqual(sorted(s.job for s in self.b.transcripts()), ["act", "new"])
        self.assertIsNone(self.b.transcript_body("old", "k1"))
        self.assertEqual(self.b.rotate_transcripts(0, 0, []), 0)   # 0 days = no time limit

    def test_transcript_rotation_by_size_drops_whole_jobs_oldest_first(self):
        import random
        rnd = random.Random(7)
        for job, key, age in (("a", "k1", 5), ("a", "k2", 1), ("b", "k3", 3), ("c", "k4", 4), ("act", "k5", 9)):
            noise = "".join(rnd.choice("abcdefghijklmnop") for _ in range(2000))
            self.b.save_transcript(self.trow(job, key, text=f'{{"v": "{noise}"}}\n', days_ago=age))
        size: dict = {}
        for s in self.b.transcripts():
            size[s.job] = size.get(s.job, 0) + s.stored_bytes
        # a job's age is its newest capture: c (4 days) goes first, then b (3), then a (1)
        limit = size["a"] + size["b"] + size["act"]
        self.assertEqual(self.b.rotate_transcripts(0, limit, ["act"]), 1)
        self.assertEqual(sorted({s.job for s in self.b.transcripts()}), ["a", "act", "b"])
        # the active job alone over the limit: every other job goes, the active one stays
        self.assertEqual(self.b.rotate_transcripts(0, 1, ["act"]), 3)
        self.assertEqual([s.job for s in self.b.transcripts()], ["act"])
        self.assertEqual(self.b.rotate_transcripts(0, 0, []), 0)   # 0 = no size limit

    # ---- transcript images
    def img(self, seed: int, mime: str = "image/png", n: int = 1000):
        import hashlib
        from support import fake_image
        data = fake_image(seed, "png" if mime == "image/png" else "jpeg", n)
        return TranscriptImage(hashlib.sha256(data).hexdigest(), mime, len(data), data)

    def irow(self, job, key, images, text=None, days_ago=None):
        import dataclasses
        text = text or "".join(f'{{"img": "{i.sha256}"}}\n' for i in images) or "{}\n"
        return dataclasses.replace(self.trow(job, key, text=text, days_ago=days_ago), images=tuple(images))

    def test_transcript_images_are_stored_once_and_referenced(self):
        a, b = self.img(1), self.img(2, "image/jpeg", 500)
        r1 = self.irow("j", "k1", [a, b])
        r2 = self.irow("j", "k2", [a])
        self.assertTrue(self.b.save_transcript(r1))
        self.assertTrue(self.b.save_transcript(r2))
        stored = self.b.transcript_images()
        self.assertEqual([(i.sha256, i.mime, i.size, i.data) for i in stored],
                         sorted([(a.sha256, a.mime, a.size, None), (b.sha256, b.mime, b.size, None)]))
        self.assertTrue(all(i.first_seen is not None and i.first_seen.tzinfo for i in stored))
        self.assertEqual(self.b.transcript_image(a.sha256).data, a.data)
        self.assertEqual(self.b.transcript_image(b.sha256).mime, "image/jpeg")
        self.assertIsNone(self.b.transcript_image("0" * 64))
        self.assertEqual([i.sha256 for i in self.b.transcript_images(job="j", agent_key="k2")], [a.sha256])
        s1 = self.b.transcripts(agent_key="k1")[0]
        self.assertEqual(s1.image_bytes, a.size + b.size)
        self.assertEqual(sorted(s1.images), sorted([(a.sha256, a.size), (b.sha256, b.size)]))
        self.assertEqual((s1.raw_bytes, s1.stored_bytes),
                         (r1.raw_bytes + a.size + b.size, len(r1.body) + a.size + b.size))
        t = self.b.transcript_totals()
        self.assertEqual((t.image_bytes, t.images, t.jobs), (a.size + b.size, 2, 1))
        self.assertEqual(t.stored, len(r1.body) + len(r2.body) + a.size + b.size)   # a once
        self.assertEqual(t.raw, r1.raw_bytes + r2.raw_bytes + a.size + b.size)

    def test_resaving_replaces_references_and_drops_orphaned_images(self):
        a, b, c = self.img(1), self.img(2), self.img(3)
        self.b.save_transcript(self.irow("j", "k1", [a, b]))
        self.b.save_transcript(self.irow("j", "k2", [b]))
        self.assertTrue(self.b.save_transcript(self.irow("j", "k1", [c])))
        self.assertEqual({i.sha256 for i in self.b.transcript_images()}, {b.sha256, c.sha256})
        self.assertIsNone(self.b.transcript_image(a.sha256))
        self.assertEqual(self.b.transcript_image(b.sha256).data, b.data)   # k2 still has it

    def test_image_keeps_first_seen(self):
        a = self.img(1)
        self.b.save_transcript(self.irow("j", "k1", [a]))
        first = self.b.transcript_images()[0].first_seen
        self.b.save_transcript(self.irow("j", "k2", [a]))
        self.assertEqual(self.b.transcript_images()[0].first_seen, first)

    def test_rotation_keeps_images_still_referenced(self):
        shared, own = self.img(1), self.img(2)
        self.b.save_transcript(self.irow("old", "k1", [shared, own], days_ago=40))
        self.b.save_transcript(self.irow("new", "k2", [shared], days_ago=1))
        self.assertEqual(self.b.rotate_transcripts(30, 0, []), 1)
        self.assertEqual([i.sha256 for i in self.b.transcript_images()], [shared.sha256])
        self.assertIsNone(self.b.transcript_image(own.sha256))
        self.b.save_transcript(self.irow("new2", "k3", [], text="{}\n", days_ago=1))
        self.assertEqual(self.b.rotate_transcripts(0, 1, ["new2"]), 1)   # by size: "new" goes
        self.assertEqual(self.b.transcript_images(), [])
        self.assertEqual(self.b.transcript_totals().images, 0)

    def test_rotation_by_size_counts_images(self):
        big = self.img(1, n=20000)
        self.b.save_transcript(self.irow("a", "k1", [big], days_ago=3))
        self.b.save_transcript(self.irow("b", "k2", [], text="{}\n", days_ago=1))
        # text alone is tiny: only the image puts the board over 10 KB
        self.assertEqual(self.b.rotate_transcripts(0, 10_000, []), 1)
        self.assertEqual([s.job for s in self.b.transcripts()], ["b"])

    def test_setup_keeps_transcripts(self):
        self.b.save_transcript(self.trow())
        type(self.b).setup(self.h.cfg, SMALL_POOL)
        self.assertEqual(len(self.b.transcripts()), 1)


    # ---- supervisor: closing stuck agents, replacements, the per-job switch (schema 6)
    def test_close_agent_records_reason_and_frees_name(self):
        name = self.b.allocate_name("k1", "j")
        self.b.tool_started("k1", "Bash")
        self.assertTrue(self.b.close_agent("k1", "stuck:tool"))
        a = self.agent("k1")
        self.assertEqual((a.status, a.left_reason, a.current_tool), ("left", "stuck:tool", None))
        self.assertIsNotNone(a.ended_at)
        self.assertFalse(self.b.close_agent("k1", "stuck:dead"))   # already departed
        self.assertNotIn(name, [x.name for x in self.b.agents("j", include_departed=False)])

    def test_close_agent_seen_before_is_a_compare_and_set(self):
        self.b.allocate_name("k1", "j")
        seen = self.agent("k1").last_contact_at
        self.b.tool_finished("k1")                 # a hook contact after the detection read
        later = self.agent("k1").last_contact_at
        self.assertGreater(later, seen)
        self.assertFalse(self.b.close_agent("k1", "stuck:dead", seen_before=seen))
        self.assertIsNone(self.agent("k1").ended_at)
        self.assertTrue(self.b.close_agent("k1", "stuck:dead", seen_before=later))

    def test_stuck_closed_row_is_never_revived(self):
        """A key the supervisor closed stays departed: allocate_name returns its name and
        changes nothing, whether or not the name is free (no revive, no full reset)."""
        self.h.reset({"simpsons": ["Homer Simpson"], "english": ["Ned"]})
        self.assertEqual(self.b.allocate_name("k1", "j", "worker"), "Homer Simpson")
        self.b.close_agent("k1", "stuck:silent")
        before = self.agent("k1")
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")
        a = self.agent("k1")
        self.assertEqual((a.status, a.left_reason, a.ended_at), ("left", "stuck:silent", before.ended_at))
        self.assertEqual(self.b.agents("j", include_departed=False), [])
        self.assertEqual(self.b.claim_resume("k2", "k1", "j"), "Homer Simpson")   # the replacement
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")      # name now held
        self.assertEqual((self.agent("k1").status, self.agent("k1").left_reason), ("left", "stuck:silent"))
        self.assertEqual([x.agent_key for x in self.b.agents("j", include_departed=False)], ["k2"])

    def test_key_named_by_a_restart_row_is_never_revived(self):
        """Even with its stuck reason cleared, a key a restart row names as the lost agent
        stays departed: no revive (name free) and no reset (name held)."""
        self.h.reset({"simpsons": ["Homer Simpson"], "english": ["Ned"]})
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")
        self.b.close_agent("k1", "stuck:dead")
        self.assertIsNotNone(self.b.record_restart("j", "k1", "k1", "stuck:dead", "claude", 60))
        self.h.update_agent("k1", left_reason=None)
        self.assertTrue(self.b.was_replaced("k1"))
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")
        self.assertIsNotNone(self.agent("k1").ended_at)
        self.assertEqual(self.b.agents("j", include_departed=False), [])
        self.assertEqual(self.b.allocate_name("k2", "j"), "Homer Simpson")   # name now held
        self.assertEqual(self.b.allocate_name("k1", "j"), "Homer Simpson")   # no full reset
        a = self.agent("k1")
        self.assertEqual((a.name, a.status), ("Homer Simpson", "left"))

    def test_was_replaced(self):
        self.b.allocate_name("k1", "j")
        self.assertFalse(self.b.was_replaced("k1"))
        self.assertFalse(self.b.was_replaced("nobody"))
        self.b.close_agent("k1", "stuck:dead")
        self.b.record_restart("j", "k1", "k1", "stuck:dead", "claude", 60)
        self.assertTrue(self.b.was_replaced("k1"))
        self.assertFalse(self.b.was_replaced("k2"))

    def test_completed_agent_is_still_revived(self):
        name = self.b.allocate_name("k1", "j")
        self.b.agent_stopped("k1")
        self.assertEqual(self.b.allocate_name("k1", "j"), name)
        a = self.agent("k1")
        self.assertEqual((a.ended_at, a.left_reason), (None, None))

    def test_claim_resume_takes_name_role_and_cursor(self):
        self.b.post("j", "someone", "before")
        name = self.b.allocate_name("old", "j", "worker")
        self.b.read_unread(agent_key="old", job="j")          # cursor past "before"
        self.b.post("j", "someone", "after")
        self.b.close_agent("old", "stuck:dead")
        self.assertEqual(self.b.claim_resume("new", "old", "j"), name)
        a = self.agent("new")
        self.assertEqual((a.name, a.role, a.resume_of, a.status, a.ended_at), (name, "worker", "old", "started", None))
        self.assertEqual([m.message for m in self.b.read_unread(agent_key="new", job="j").messages], ["after"])
        self.assertEqual(self.b.claim_resume("new", "old", "j"), name)   # idempotent

    def test_claim_resume_refuses_when_name_is_held_or_old_is_active(self):
        self.h.reset({"simpsons": ["Homer Simpson"], "english": []})
        name = self.b.allocate_name("old", "j")
        self.assertIsNone(self.b.claim_resume("new", "old", "j"))      # old still active
        self.b.close_agent("old", "stuck:dead")
        self.assertEqual(self.b.allocate_name("holder", "j"), name)   # someone else took the name
        self.assertIsNone(self.b.claim_resume("new", "old", "j"))
        self.assertIsNone(self.b.claim_resume("new", "missing", "j"))
        self.assertFalse(any(a.agent_key == "new" for a in self.b.agents("j")))

    def test_claim_resume_carries_judge_and_verifier_marks(self):
        self.b.allocate_name("jd", "j")
        self.assertTrue(self.b.claim_judge("jd", "j"))
        self.b.allocate_name("vf", "j")
        self.assertTrue(self.b.claim_verifier("vf", "j"))
        self.b.close_agent("jd", "stuck:dead")
        self.b.close_agent("vf", "stuck:dead")
        self.b.claim_resume("jd2", "jd", "j")
        self.b.claim_resume("vf2", "vf", "j")
        self.assertEqual((self.agent("jd2").role, self.agent("vf2").role), ("judge", "verifier"))
        self.assertEqual(self.b.job_status("j").judge, self.agent("jd2").name)

    def test_job_supervise_flag(self):
        self.b.open_job("j", None, None, None, None)
        self.assertTrue(self.b.job_status("j").supervise)
        self.assertTrue(self.b.set_job_supervise("j", False))
        self.assertFalse(self.b.job_status("j").supervise)
        self.assertFalse(self.b.set_job_supervise("nope", False))

    # ---- supervisor restarts
    def _restart(self, old="old", root="root", reason="stuck:dead", **kw):
        return self.b.record_restart("j", root, old, reason, "claude", 60.0, **kw)

    def test_record_restart_numbers_attempts_per_lineage(self):
        r1 = self._restart(old="root")
        r2 = self._restart(old="rep1")
        other = self._restart(old="x", root="other")
        self.assertEqual((r1.attempt, r2.attempt, other.attempt), (1, 2, 1))
        self.assertEqual((r1.outcome, r1.ended_at, r1.new_agent_key), (None, None, None))
        import getpass, os
        self.assertEqual((r1.host, r1.os_user, r1.harness, r1.minutes_cap),
                         (compat.node(), getpass.getuser(), "claude", 60.0))

    def test_record_restart_once_per_closed_agent(self):
        self.assertIsNotNone(self._restart(old="k"))
        self.assertIsNone(self._restart(old="k"))                 # a second supervisor loses
        self.assertEqual(len(self.b.restarts(job="j")), 1)

    def test_record_restart_enforces_the_job_cap(self):
        self.assertIsNotNone(self._restart(old="a", root="ra", max_per_job=2))
        self.assertIsNotNone(self._restart(old="b", root="rb", max_per_job=2))
        self.assertIsNone(self._restart(old="c", root="rc", max_per_job=2))      # at the cap: nothing
        self.assertIsNone(self._restart(old="d", root="rd", max_per_job=2, outcome="refused"))
        self.assertEqual([r.old_agent_key for r in self.b.restarts(job="j")], ["a", "b"])
        self.assertIsNotNone(self.b.record_restart("j2", "r", "e", "stuck:dead", "claude", 1.0, max_per_job=1))
        self.assertIsNotNone(self._restart(old="f", root="rf"))                  # no cap given: not enforced

    def test_record_restart_job_cap_holds_under_concurrency(self):
        import threading
        got, errors = [], []

        def one(i):
            try:
                with self.h.board() as b:
                    got.append(b.record_restart("j", f"r{i}", f"old{i}", "stuck:dead", "claude", 1.0,
                                                max_per_job=1))
            except Exception as exc:   # pragma: no cover - reported below
                errors.append(exc)
        ts = [threading.Thread(target=one, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in got if r is not None), 1)
        self.assertEqual(len(self.b.restarts(job="j")), 1)

    def test_record_restart_enforces_the_job_minutes(self):
        """The per-job minute cap is checked with the insert, atomically."""
        rec = lambda old, cap, **kw: self.b.record_restart("j", f"r{old}", old, "stuck:dead", "claude", cap,
                                                           max_job_minutes=180, **kw)
        a = rec("a", 150.0)
        self.assertIsNotNone(a)
        self.assertIsNotNone(rec("b", 30.0))                  # 150 + 30 = 180: still within
        self.assertIsNone(rec("c", 1.0))                      # 181 > 180: nothing recorded
        self.assertTrue(self.b.finish_restart(a.id, "failed"))   # ended at once: charged ~0
        self.assertIsNotNone(rec("d", 100.0))
        self.assertIsNotNone(rec("e", 1.0, outcome="refused"))   # a refusal reserves nothing
        self.assertEqual([r.old_agent_key for r in self.b.restarts(job="j")], ["a", "b", "d", "e"])
        self.assertIsNotNone(self.b.record_restart("j2", "r", "f", "stuck:dead", "claude", 180.0,
                                                   max_job_minutes=180))   # per job

    def test_record_restart_job_minutes_hold_under_concurrency(self):
        import threading
        got, errors = [], []

        def one(i):
            try:
                with self.h.board() as b:
                    got.append(b.record_restart("j", f"r{i}", f"old{i}", "stuck:dead", "claude", 50.0,
                                                max_job_minutes=180))
            except Exception as exc:   # pragma: no cover - reported below
                errors.append(exc)
        ts = [threading.Thread(target=one, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in got if r is not None), 3)   # 3 x 50 = 150; a 4th would be 200
        self.assertEqual(len(self.b.restarts(job="j")), 3)

    def test_record_restart_enforces_host_caps_across_os_users(self):
        """Concurrency and daily minutes are per host, whichever OS user's
        supervisor started the rows, and counted with the insert."""
        import datetime as dt
        from unittest import mock
        day = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        kw = dict(max_host_running=2, max_host_minutes=150, day_start=day)
        with mock.patch("getpass.getuser", return_value="codex-user"):
            self.assertIsNotNone(self.b.record_restart("j1", "r1", "o1", "stuck:dead", "codex", 60.0, **kw))
        self.assertIsNotNone(self.b.record_restart("j2", "r2", "o2", "stuck:dead", "claude", 60.0, **kw))
        self.assertIsNone(self.b.record_restart("j3", "r3", "o3", "stuck:dead", "claude", 10.0, **kw))  # 2 running
        r1 = self.b.restarts(job="j1")[0]
        self.assertTrue(self.b.finish_restart(r1.id, "failed"))
        self.assertIsNone(self.b.record_restart("j3", "r3", "o3", "stuck:dead", "claude", 100.0, **kw))  # 60+100 > 150
        self.assertIsNotNone(self.b.record_restart("j3", "r3", "o3", "stuck:dead", "claude", 89.0, **kw))
        self.assertIsNotNone(self.b.record_restart("j4", "r4", "o4", "stuck:dead", "claude", 1.0,
                                                   outcome="refused", **kw))   # refusals always recorded

    def test_record_restart_host_running_cap_holds_under_concurrency(self):
        import threading
        got, errors = [], []

        def one(i):
            try:
                with self.h.board() as b:
                    got.append(b.record_restart(f"j{i}", f"r{i}", f"old{i}", "stuck:dead", "claude", 1.0,
                                                max_host_running=2))
            except Exception as exc:   # pragma: no cover - reported below
                errors.append(exc)
        ts = [threading.Thread(target=one, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)
            self.assertFalse(t.is_alive(), "board worker did not finish before fixture cleanup")
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in got if r is not None), 2)

    def test_refused_restart_is_recorded_ended(self):
        r = self._restart(old="k", outcome="refused")
        self.assertEqual(r.outcome, "refused")
        self.assertIsNotNone(r.ended_at)

    def test_set_agent_and_finish(self):
        r = self._restart()
        self.assertTrue(self.b.set_restart_agent(r.id, "new-key"))
        self.assertTrue(self.b.finish_restart(r.id, "completed"))
        self.assertFalse(self.b.finish_restart(r.id, "failed"))   # first finish wins
        got = self.b.restarts(agent_key="root")[0]
        self.assertEqual((got.new_agent_key, got.outcome), ("new-key", "completed"))
        self.assertIsNotNone(got.ended_at)
        with self.assertRaises(ValueError):
            self.b.finish_restart(r.id, "exploded")

    def test_restarts_filters(self):
        import getpass, os
        self._restart(old="a")
        self.b.record_restart("j2", "r2", "b", "stuck:silent", "codex", 30.0)
        self.assertEqual([r.old_agent_key for r in self.b.restarts(job="j2")], ["b"])
        mine = self.b.restarts(host=compat.node(), os_user=getpass.getuser())
        self.assertEqual(len(mine), 2)
        self.assertEqual(self.b.restarts(host="elsewhere"), [])
        future = self.b.now() + dt.timedelta(minutes=1)
        self.assertEqual(self.b.restarts(since=future), [])

    def test_purge_drops_restarts_older_than_retention(self):
        self._restart(old="a")
        self.b.purge()
        self.assertEqual(len(self.b.restarts()), 1)
        b0 = self.h.board(retention_days=0)          # now - 0 days: every recorded row is older
        self.addCleanup(b0.close)
        b0.purge()
        self.assertEqual(self.b.restarts(), [])

    # ---- terminal controls (write side) and agent names
    CONTROLS = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
    BAD_NAMES = ("Lisa\n[swarm] forged line", "Lisa\r[swarm]", "x\x1b[31m", "Li\u202esa", "Lisa\t",
                 "Lisa\x85", "", " Lisa", "Lisa ", "a" * 65, "Zo\u00eb", "Li\u200bsa", "a/b", "a:b")

    def test_post_strips_controls(self):
        self.b.post("j", "Alice", "a\x1b[2Jb\x07c\x9bd \u202eevil\u202c e\x00f\u2028g\u2066h")
        [m] = self.b.messages_after(0, "j")
        self.assertIsNone(self.CONTROLS.search(m.message), repr(m.message))
        self.assertEqual(m.message, "a[2Jbcd evil ef gh")

    def test_post_of_controls_only_is_empty(self):
        with self.assertRaises(ValueError):
            self.b.post("j", "Alice", "\x1b\x07\x9b")
        self.assertEqual(self.b.messages_after(0), [])

    def test_name_with_newline_rejected(self):
        self.b.allocate_name("jk", "j")
        self.assertTrue(self.b.claim_judge("jk", "j"))
        for bad in self.BAD_NAMES:
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    self.b.post("j", bad, "hello")
                with self.assertRaises(ValueError):
                    self.b.post("j", "Alice", "hello", to=bad)
                with self.assertRaises(ValueError):
                    self.b.record_verdict("j", bad, "met", "ok")
        self.assertEqual(self.b.messages_after(0), [])
        self.assertIsNone(self.b.job_status("j").verdict)

    def test_valid_names_accepted(self):
        for good in ("Dr. J. Loren Pryor", "O'Brien", "a_b-c 123", "x" * 64, "Alice", "Q"):
            self.b.post("j", good, "hi", to=good)
        self.assertEqual(len(self.b.messages_after(0, "j")), 6)

    def test_setup_skips_invalid_pool_names(self):
        from swarm.board import setup_board
        before = setup_board(self.h.cfg, {"simpsons": [], "english": []}).pool
        after = setup_board(self.h.cfg, {"simpsons": ["Bad\nName", " Lead"], "english": ["x\x1b"]}).pool
        self.assertEqual(after, before)

    # ---- memory provenance (schema 8)
    def mref(self, doc="doc-1", key="k1", **kw):
        import lzma
        fields = dict(document_id=doc, bank="notes", job="j", agent_key=key, agent_name="Homer Simpson",
                      harness="claude", host="h1", session_id="s1", tool_call_id="toolu_1", writer="note-tool",
                      excerpt=lzma.compress(b'{"type":"user"}\n'), raw_bytes=16, redactions=2)
        fields.update(kw)
        return MemoryRef(**fields)

    def image(self, seed=1):
        import hashlib
        data = fake_image(seed)
        return TranscriptImage(hashlib.sha256(data).hexdigest(), "image/png", len(data), data)

    def test_memory_ref_round_trip(self):
        self.assertEqual(self.b.save_memory_ref(self.mref()), "inserted")
        [r] = self.b.memory_refs(job="j")
        self.assertEqual((r.document_id, r.bank, r.agent_name, r.tool_call_id, r.writer, r.harness, r.host,
                          r.session_id, r.raw_bytes, r.redactions, r.patched),
                         ("doc-1", "notes", "Homer Simpson", "toolu_1", "note-tool", "claude", "h1", "s1", 16, 2, False))
        self.assertIsNone(r.excerpt)
        self.assertIsNotNone(r.created_at)
        self.assertIsNotNone(r.created_at.tzinfo)
        self.assertIsNone(r.checked_at)
        self.assertGreater(r.stored_bytes, 0)
        self.assertEqual(self.b.memory_ref_excerpt("doc-1"), b'{"type":"user"}\n')
        self.assertIsNone(self.b.memory_ref_excerpt("nope"))

    def test_memory_ref_without_excerpt(self):
        self.b.save_memory_ref(self.mref(excerpt=None, raw_bytes=0))
        self.assertIsNone(self.b.memory_ref_excerpt("doc-1"))
        self.assertEqual(self.b.memory_refs()[0].stored_bytes, 0)

    def test_memory_ref_first_writer_wins_same_agent_updates(self):
        self.b.save_memory_ref(self.mref())
        self.assertEqual(self.b.save_memory_ref(self.mref(key="k2", agent_name="Bart", tool_call_id="toolu_2")), "kept")
        self.assertEqual(self.b.memory_refs(document_id="doc-1")[0].agent_key, "k1")
        self.assertEqual(self.b.save_memory_ref(self.mref(tool_call_id="toolu_3", patched=True)), "updated")
        r = self.b.memory_refs(document_id="doc-1")[0]
        self.assertEqual((r.tool_call_id, r.patched), ("toolu_3", True))

    def test_memory_ref_update_resets_checked_at_and_replaces_excerpt(self):
        import lzma
        self.b.save_memory_ref(self.mref())
        self.b.mark_memory_refs_checked(["doc-1"])
        self.b.save_memory_ref(self.mref(excerpt=lzma.compress(b"second\n"), raw_bytes=7))
        self.assertIsNone(self.b.memory_refs()[0].checked_at)
        self.assertEqual(self.b.memory_ref_excerpt("doc-1"), b"second\n")
        self.b.save_memory_ref(self.mref(excerpt=None, raw_bytes=0))
        self.assertIsNone(self.b.memory_ref_excerpt("doc-1"))

    def test_memory_ref_filters_and_order(self):
        self.b.save_memory_ref(self.mref("d1"))
        self.b.save_memory_ref(self.mref("d2", key="k2", agent_name="Bart", job="other"))
        self.assertEqual([r.document_id for r in self.b.memory_refs()], ["d1", "d2"])
        self.assertEqual([r.document_id for r in self.b.memory_refs(agent_name="Bart")], ["d2"])
        self.assertEqual([r.document_id for r in self.b.memory_refs(agent_key="k1")], ["d1"])
        self.assertEqual([r.document_id for r in self.b.memory_refs(job="other")], ["d2"])
        self.assertEqual(self.b.memory_refs(document_id="zz"), [])

    def test_memory_ref_images_outlive_transcript_rotation(self):
        import lzma
        img = self.image(7)
        row = TranscriptRow(job="j", agent_key="k1", agent_name="Homer Simpson", role="subagent", host=None,
                            session_id=None, final=True, raw_bytes=2, redactions=0, sha256="a" * 64,
                            body=lzma.compress(b"x\n"), images=(img,))
        self.assertTrue(self.b.save_transcript(row))
        self.b.save_memory_ref(self.mref(images=(img,)))
        self.assertEqual(self.b.rotate_transcripts(0, 1, []), 1)       # the size limit drops job j
        self.assertEqual(self.b.transcripts(job="j"), [])
        self.assertEqual(self.b.transcript_image(img.sha256).data, img.data)
        [ri] = self.b.memory_refs(document_id="doc-1")[0].images
        self.assertEqual((ri.sha256, ri.mime, ri.size, ri.data), (img.sha256, "image/png", img.size, None))
        self.assertEqual(self.b.delete_memory_refs(["doc-1", "nope"]), 1)
        self.assertEqual(self.b.memory_refs(), [])
        self.assertIsNone(self.b.transcript_image(img.sha256))

    def test_memory_ref_image_shared_with_a_transcript_stays_with_it(self):
        import lzma
        img = self.image(8)
        self.b.save_memory_ref(self.mref(images=(img,)))
        row = TranscriptRow(job="j", agent_key="k1", agent_name="Homer Simpson", role="subagent", host=None,
                            session_id=None, final=True, raw_bytes=2, redactions=0, sha256="b" * 64,
                            body=lzma.compress(b"x\n"), images=(img,))
        self.assertTrue(self.b.save_transcript(row))
        self.assertEqual(self.b.delete_memory_refs(["doc-1"]), 1)
        self.assertEqual(self.b.transcript_image(img.sha256).data, img.data)   # the transcript still has it

    def test_memory_ref_update_releases_its_old_images(self):
        a, b = self.image(1), self.image(2)
        self.b.save_memory_ref(self.mref(images=(a,)))
        self.b.save_memory_ref(self.mref(images=(b,)))
        self.assertIsNone(self.b.transcript_image(a.sha256))
        self.assertIsNotNone(self.b.transcript_image(b.sha256))

    def test_mark_memory_refs_checked(self):
        self.b.save_memory_ref(self.mref("d1"))
        self.b.save_memory_ref(self.mref("d2"))
        self.b.mark_memory_refs_checked(["d1", "nope"])
        got = {r.document_id: r.checked_at for r in self.b.memory_refs()}
        self.assertIsNotNone(got["d1"])
        self.assertIsNone(got["d2"])

    def test_delete_memory_refs_only_if_unchanged(self):
        """provenance.prune deletes what it read, not a row re-recorded or re-checked since: a
        row whose created_at or checked_at no longer matches `expected` stays."""
        import datetime as dt
        t0 = dt.datetime(2026, 9, 1, 12, 0, 0, 123456, tzinfo=dt.timezone.utc)
        for d in ("d1", "d2", "d3", "d4"):
            self.b.save_memory_ref(self.mref(d, created_at=t0))
        snap = {r.document_id: (r.created_at, r.checked_at) for r in self.b.memory_refs()}
        self.b.save_memory_ref(self.mref("d2", created_at=t0 + dt.timedelta(seconds=5)))   # re-recorded
        self.b.mark_memory_refs_checked(["d3"])                                           # checked since
        self.assertEqual(self.b.delete_memory_refs(["d1", "d2", "d3", "d4", "nope"],
                                                   expected={**snap, "d4": (t0, t0), "nope": (t0, None)}), 1)
        self.assertEqual({r.document_id for r in self.b.memory_refs()}, {"d2", "d3", "d4"})
        self.assertEqual(self.b.delete_memory_refs(["d3"], expected={}), 0)   # no expectation: kept
        self.assertEqual(len(self.b.memory_refs()), 3)

    def test_purge_leaves_memory_refs(self):
        self.b.ensure_job("j")
        self.h.backdate_job("j", created_at=30 * DAY)
        self.b.save_memory_ref(self.mref())
        self.b.purge()
        self.assertIsNone(self.b.job_status("j"))
        self.assertEqual([r.document_id for r in self.b.memory_refs()], ["doc-1"])

    def test_memory_excerpt_decompression_is_capped(self):
        # a forged row (written past save_memory_ref's checks) is refused on read
        import lzma
        from swarm.board import EXCERPT_MAX_RAW
        self.b._save_memory_ref(self.mref(excerpt=lzma.compress(b"a" * (EXCERPT_MAX_RAW + 1))))
        with self.assertRaises(BoardError):
            self.b.memory_ref_excerpt("doc-1")

    def test_corrupt_memory_excerpt_is_a_board_error(self):
        self.b._save_memory_ref(self.mref(excerpt=b"not lzma at all"))
        with self.assertRaises(BoardError):
            self.b.memory_ref_excerpt("doc-1")

    def test_memory_ref_excerpt_checked_on_save(self):
        import lzma
        from swarm.board import EXCERPT_MAX_RAW
        big = b"a" * (EXCERPT_MAX_RAW + 1)
        for label, kw in (("over the cap", dict(excerpt=lzma.compress(big), raw_bytes=len(big))),
                          ("not lzma", dict(excerpt=b"not lzma at all", raw_bytes=15)),
                          ("cut short", dict(excerpt=lzma.compress(b"x" * 100)[:-8], raw_bytes=100)),
                          ("raw_bytes too small", dict(raw_bytes=15)),
                          ("raw_bytes too large", dict(raw_bytes=17)),
                          ("no excerpt, raw_bytes set", dict(excerpt=None, raw_bytes=16))):
            with self.subTest(label), self.assertRaises(ValueError):
                self.b.save_memory_ref(self.mref(**kw))
        self.assertEqual(self.b.memory_refs(), [])
        exact = b"a" * EXCERPT_MAX_RAW
        self.assertEqual(self.b.save_memory_ref(self.mref(excerpt=lzma.compress(exact), raw_bytes=len(exact))),
                         "inserted")

    def test_memory_ref_images_capped(self):
        import dataclasses
        import hashlib
        from swarm.board import MEMORY_REF_IMAGE_BYTES_MAX, MEMORY_REF_IMAGES_MAX

        def img(data):
            return TranscriptImage(hashlib.sha256(data).hexdigest(), "image/png", len(data), data)
        many = tuple(img(fake_image(i, n=100)) for i in range(MEMORY_REF_IMAGES_MAX + 1))
        with self.assertRaises(ValueError):
            self.b.save_memory_ref(self.mref(images=many))
        half = MEMORY_REF_IMAGE_BYTES_MAX // 2
        big = (img(fake_image(1, n=half)), img(fake_image(2, n=half)))   # magic bytes tip it over
        with self.assertRaises(ValueError):
            self.b.save_memory_ref(self.mref(images=big))
        lying = dataclasses.replace(img(fake_image(3, n=100)), size=5)   # size must be len(data)
        with self.assertRaises(ValueError):
            self.b.save_memory_ref(self.mref(images=(lying,)))
        self.assertEqual(self.b.memory_refs(), [])
        self.assertEqual(self.b.save_memory_ref(self.mref(images=many[:MEMORY_REF_IMAGES_MAX])), "inserted")

    def test_unknown_memory_writer_refused(self):
        with self.assertRaises(ValueError):
            self.b.save_memory_ref(self.mref(writer="bad name!"))
        self.assertEqual(self.b.memory_refs(), [])

    def test_memory_ref_bad_agent_name_refused(self):
        for bad in self.BAD_NAMES:
            with self.subTest(name=bad), self.assertRaises(ValueError):
                self.b.save_memory_ref(self.mref(agent_name=bad))
        self.assertEqual(self.b.memory_refs(), [])

    def test_memory_ref_image_sha256_is_recomputed(self):
        import dataclasses
        img = self.image(3)
        for bad in (dataclasses.replace(img, sha256="../" + "a" * 61), dataclasses.replace(img, sha256="0" * 64),
                    dataclasses.replace(img, sha256=img.sha256.upper())):
            with self.subTest(sha=bad.sha256), self.assertRaises(ValueError):
                self.b.save_memory_ref(self.mref(images=(bad,)))
        self.assertEqual(self.b.memory_refs(), [])


class MemoryBoardContract(BoardContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteBoardContract(BoardContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileBoardContract(BoardContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresBoardContract(BoardContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresSpecificTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def test_agent_status_job_filter_reaches_message_scan(self):
        from swarm.board.postgres import _AGENT_STATUS_COLS
        self.b.allocate_name("one", "j")
        self.b.allocate_name("two", "other")
        self.b.post("j", self.b.active_agent_name("one"), "local")
        self.b.post("other", self.b.active_agent_name("two"), "unrelated")
        plan = self.h.conn.execute(f"EXPLAIN (FORMAT JSON) SELECT {_AGENT_STATUS_COLS} "
                                   "FROM agent_status WHERE job = 'j'").fetchone()[0][0]["Plan"]

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        scans = [node for node in nodes(plan) if node.get("Relation Name") == "messages"]
        self.assertTrue(scans, plan)
        for scan in scans:
            # A bitmap scan may carry the condition on its child index scan instead.
            conditions = " ".join(str(n.get(field, "")) for n in nodes(scan)
                                  for field in ("Filter", "Index Cond", "Recheck Cond"))
            self.assertIn("job", conditions, plan)
            self.assertTrue("'j'" in conditions or "a.job" in conditions, plan)
        agent_scans = [node for node in nodes(plan) if node.get("Relation Name") == "agents"]
        self.assertTrue(agent_scans, plan)
        for scan in agent_scans:
            conditions = " ".join(str(n.get(field, "")) for n in nodes(scan)
                                  for field in ("Filter", "Index Cond", "Recheck Cond"))
            self.assertIn("'j'", conditions, plan)

    def test_multi_job_listing_totals_are_parameterized_by_job(self):
        from swarm.board.postgres import _JOB_STATUS_COLS
        for job in ("one", "two", "other"):
            self.b.open_job(job, None, None, "S1" if job != "other" else "S2", None)
            self.b.allocate_name(job, job)
            self.b.post(job, self.b.active_agent_name(job), "counted")
        queries = (
            f"SELECT {_JOB_STATUS_COLS} FROM job_status WHERE job = ANY(%s)",
            f"SELECT {_JOB_STATUS_COLS} FROM job_status WHERE session_id = %s",
            f"SELECT {_JOB_STATUS_COLS} FROM job_status WHERE status = 'active'",
        )
        for query, params in zip(queries, ((["one", "two"],), ("S1",), ())):
            with self.subTest(query=query):
                plan = self.h.conn.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()[0][0]["Plan"]

                def nodes(node):
                    yield node
                    for child in node.get("Plans", []):
                        yield from nodes(child)

                for relation in ("messages", "agents"):
                    scans = [n for n in nodes(plan) if n.get("Relation Name") == relation]
                    self.assertTrue(scans, plan)
                    for scan in scans:
                        conditions = " ".join(str(n.get(field, "")) for n in nodes(scan)
                                              for field in ("Filter", "Index Cond", "Recheck Cond"))
                        self.assertIn("j.job", conditions, plan)

    def test_session_watch_snapshot_reads_only_selected_jobs(self):
        from swarm import cli
        self.b.open_job("old", None, None, "S1", None)
        self.b.close_job("old", "completed", None)
        for job, session in (("one", "S1"), ("two", "S1"), ("other", "S2")):
            self.b.open_job(job, None, None, session, None)
            self.b.allocate_name(job, job)
            self.b.post(job, self.b.active_agent_name(job), job)
        for compact in (False, True):
            view = {"session": "S1", "compact": compact, "recent_minutes": 10}
            with mock.patch.object(self.b._conn, "execute", wraps=self.b._conn.execute) as execute:
                snap = cli._take_snapshot(self.b, lambda b: b.session_jobs("S1"), view, None)
                self.assertEqual(execute.call_count, 1)
            self.assertEqual([j.job for j in snap._board.job_rows], ["one", "two"])
            self.assertEqual({a.job for a in snap._board.agent_rows}, {"one", "two"})
            self.assertEqual({m.job for m in snap._board.message_rows}, {"one", "two"})
        self.b.close_job("one", "completed", None)
        self.b.close_job("two", "completed", None)
        self.assertEqual([j.job for j in self.b.watch_snapshot(None, "S1", 10, 60).job_rows], ["two"])
        self.assertEqual(self.b.watch_snapshot(None, "missing", 10, 60).job_rows, [])

    def test_schema16_to_19_recreates_status_views_without_changing_results(self):
        self._assert_status_view_migration(16, "schema15_status_views.sql")

    def test_schema17_to_19_recreates_status_views_without_changing_results(self):
        self._assert_status_view_migration(17, "schema17_status_views.sql")

    def test_schema18_to_19_preserves_blocker_views_and_results(self):
        self._assert_status_view_migration(18, "schema18_status_views.sql")

    def _assert_status_view_migration(self, version, fixture):
        from pathlib import Path
        from swarm.board.base import SCHEMA_VERSION
        from swarm.board.postgres import _AGENT_STATUS_COLS, _JOB_STATUS_COLS
        conn = self.h.conn
        self.b.open_job("j", "open", None, None, None)
        self.b.open_job("closed", "old", None, None, None)
        self.b.ensure_job("empty")
        for key in ("started", "running", "idle", "dead", "done", "left"):
            self.b.allocate_name(key, "j")
        self.b.tool_started("running", "Read")
        self.h.backdate_agent("idle", last_seen=6 * MIN)
        self.h.backdate_agent("dead", last_seen=31 * MIN)
        self.b.agent_stopped("done")
        self.b.leave(agent_key="left")
        self.b.allocate_name("old", "closed")
        self.b.close_job("closed", "completed", "kept")
        self.b.post("j", self.b.active_agent_name("started"), "counted")
        self.b.post("closed", "X", "historical")
        if version == 18:
            blocker = self.b.open_blocker("j", "question", "human", "choose")
            self.b.comment_blocker(blocker.id, "pending", actor="human")
            self.b.post("j", "swarm", "Blocker 99 expired: default")
        old_views = (Path(__file__).parent / "fixtures" / fixture).read_text()
        conn.execute(old_views.format(idle=5, dead=30, tool_timeout=60))
        conn.execute("UPDATE board_meta SET value = %s WHERE key = 'schema_version'", (str(version),))
        self.b.set_job_data("j", "engineering-team.optional", "build_engineer")
        legacy_job_cols = ", ".join(c.strip() for c in _JOB_STATUS_COLS.split(",")
                                    if version == 18 or c.strip() not in {"open_blockers", "protected_blockers"})
        queries = (f"SELECT {_AGENT_STATUS_COLS} FROM agent_status ORDER BY job, agent_key",
                   f"SELECT {legacy_job_cols}, shown_status FROM job_status ORDER BY job")
        before = [conn.execute(q).fetchall() for q in queries]
        columns = conn.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                               "WHERE table_name IN ('agent_status', 'job_status') "
                               "AND column_name NOT IN ('blockers', 'open_blockers', 'protected_blockers') "
                               "ORDER BY table_name, ordinal_position").fetchall()
        for _ in range(2):  # migration and idempotent re-init
            type(self.b).setup(self.h.cfg, SMALL_POOL)
            self.assertEqual(SCHEMA_VERSION, 19)
            self.assertEqual(type(self.b).schema_version(self.h.cfg), 19)
            self.assertEqual(self.b.job_data("j"), {"engineering-team.optional": "build_engineer"})
            self.assertEqual([conn.execute(q).fetchall() for q in queries], before)
            self.assertEqual(conn.execute("SELECT table_name, column_name, data_type "
                                          "FROM information_schema.columns "
                                          "WHERE table_name IN ('agent_status', 'job_status') "
                                          "AND column_name NOT IN ('blockers', 'open_blockers', 'protected_blockers') "
                                          "ORDER BY table_name, ordinal_position").fetchall(), columns)

    def test_ids_become_visible_in_order(self):
        """A poster whose id is lower but whose commit is later must not be skipped. Another
        poster holds the insert lock with an uncommitted lower id; our post must wait for it,
        so a reader in between sees neither, and afterwards sees both."""
        import psycopg
        from swarm.board.postgres import POST_LOCK
        self.b.allocate_name("k1", "j")
        self.b.ensure_job("j")
        db = self.h.cfg["database"]
        slow = psycopg.connect(host=db["host"], port=db["port"], user=db["user"],
                               password=self.h._password(db), dbname=db["dbname"], sslmode=db["sslmode"])
        try:
            slow.execute("SELECT pg_advisory_xact_lock(%s)", (POST_LOCK,))
            low = slow.execute("INSERT INTO messages (job, agent_name, message) VALUES ('j', 'Slow', 'low') "
                               "RETURNING id").fetchone()[0]
            result = {}

            def fast_post():
                with self.h.board() as b:
                    result["pid"] = b._conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                    result["id"] = b.post("j", "Fast", "high").id

            poster = threading.Thread(target=fast_post)
            poster.start()
            def waiting_on_insert_lock():
                pid = result.get("pid")
                if pid is None:
                    return False
                row = self.b._conn.execute(
                    "SELECT wait_event FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
                return row and row[0] == "advisory"
            wait_until(waiting_on_insert_lock)
            first = [m.id for m in self.b.read_new(agent_key="k1")]
            slow.commit()
        finally:
            slow.close()
        poster.join(30)
        self.assertFalse(poster.is_alive())
        second = [m.id for m in self.b.read_new(agent_key="k1")]
        self.assertEqual(first + second, [low, result["id"]])

    def test_setup_migrates_an_old_schema_in_place(self):
        """init on a database created by the previous release adds the new columns and keeps
        the data (additive migration)."""
        self.b.allocate_name("k1", "j")
        self.b.post("j", "Other", "kept")
        cols = ("roster_seen", "roster_synced_at", "memory_recalled_at", "memory_seen",
                "remembered_at", "nudged_at", "reply_reminded_id", "calls_at_post", "silence_nudged_at")
        conn = self.h.conn
        for c in cols:
            conn.execute(f"ALTER TABLE agents DROP COLUMN IF EXISTS {c} CASCADE")
        conn.execute("DROP VIEW IF EXISTS job_status")
        conn.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS project")
        conn.execute("ALTER TABLE agents DROP COLUMN IF EXISTS judge CASCADE")
        for c in ("goal", "verdict", "verdict_reason", "verdict_by", "verdict_at", "completion_forced",
                  "closed_by", "verdict_next"):
            conn.execute(f"ALTER TABLE jobs DROP COLUMN IF EXISTS {c} CASCADE")
        conn.execute("DROP TABLE IF EXISTS agent_routes")
        type(self.b).setup(self.h.cfg, SMALL_POOL)
        have = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                           "WHERE table_name = 'agents'")}
        self.assertTrue(set(cols) | {"judge"} <= have)
        have_jobs = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                                "WHERE table_name = 'jobs'")}
        self.assertTrue({"goal", "verdict", "verdict_reason", "verdict_by", "verdict_at",
                         "completion_forced", "closed_by", "verdict_next"} <= have_jobs)
        s = self.b.job_status("j")
        self.assertEqual((s.goal, s.verdict, s.judge, s.completion_forced, s.closed_by),
                         (None, None, None, False, None))
        self.assertEqual(self.b.route("k1").member_job, "j")
        self.assertEqual([m.message for m in self.b.recent_messages(5, "j")], ["kept"])
        self.assertEqual(self.b.sync_state("k1").memory_seen, ())
        self.assertIsNone(self.b.job_status("j").project)


class MemorySpecificTests(unittest.TestCase):
    def test_unavailable_store_raises_board_unavailable(self):
        from swarm.board import BoardUnavailable, open_board, memory
        h = MemoryHarness("unavailable-test")
        h.reset()
        h.store.available = False
        with self.assertRaises(BoardUnavailable) as cm:
            open_board(h.cfg)
        self.assertIsInstance(cm.exception, BoardError)
        self.assertEqual(type(cm.exception.__cause__).__name__, "ConnectionError")
        memory.reset_store("unavailable-test")

    def test_unknown_backend(self):
        from swarm.board import open_board
        from support import base_config
        with self.assertRaises(BoardError):
            open_board(base_config(backend="nope"))


class ValidNameTests(unittest.TestCase):
    def test_rule(self):
        from swarm.board.base import check_name, valid_name
        from support import base_config  # noqa: F401
        import json
        from pathlib import Path
        data = Path(__file__).resolve().parent.parent / "data"
        for f in data.glob("*_names.json"):
            for n in json.loads(f.read_text()):
                self.assertTrue(valid_name(n), n)
        for bad in BoardContract.BAD_NAMES + (None, 5, b"Lisa"):
            self.assertFalse(valid_name(bad), repr(bad))
            with self.assertRaises(ValueError):
                check_name(bad)
        self.assertEqual(check_name("Homer Simpson"), "Homer Simpson")


class SchemaUpgradeTests:
    """Schema version 7 : names and image sha256 are checked by the
    store too. Upgrading a version-6 board whose rows break the new rules must work (Postgres:
    CHECK ... NOT VALID; SQLite: BEFORE INSERT/UPDATE triggers), leave the old rows readable,
    and refuse new bad rows written directly, past the Board API."""

    BAD = "Lisa\n[swarm] forged"

    def setUp(self):
        self.h = self.harness_factory()
        self.addCleanup(self.h.close)
        self.h.reset()

    def test_schema_upgrade_adds_name_check(self):
        from swarm.board import SCHEMA_VERSION, setup_board
        self.assertGreaterEqual(SCHEMA_VERSION, 7)
        self.downgrade_with_bad_rows()
        setup_board(self.h.cfg, SMALL_POOL)
        cls = type(self.h.board())
        self.assertEqual(cls.schema_version(self.h.cfg), SCHEMA_VERSION)
        with self.h.board() as b:
            self.assertIn(self.BAD, [m.agent_name for m in b.messages_after(0)])
        self.check_new_bad_rows_refused()


class MemorySchemaUpgrade(SchemaUpgradeTests, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness("schema-upgrade"))

    def downgrade_with_bad_rows(self):
        self.h.board().post("j", "Alice", "hi")
        with self.h.store.lock:
            self.h.store.schema_version = 6
            self.h.store.messages[-1]["agent_name"] = self.BAD

    def check_new_bad_rows_refused(self):
        with self.h.board() as b, self.assertRaises(ValueError):
            b.post("j", self.BAD, "x")


class FileSchemaUpgrade(MemorySchemaUpgrade):
    harness_factory = FileHarness

    def downgrade_with_bad_rows(self):
        super().downgrade_with_bad_rows()
        from pathlib import Path
        (Path(self.h.cfg["file"]["path"]) / "schema_version").write_text("6\n")


class SqliteSchemaUpgrade(SchemaUpgradeTests, unittest.TestCase):
    harness_factory = SqliteHarness

    def downgrade_with_bad_rows(self):
        c = self.h._db()
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                 "AND name LIKE 'check_%'").fetchall():
            c.execute(f"DROP TRIGGER {name}")
        c.execute("PRAGMA user_version = 6")
        now = "2026-01-01T00:00:00.000000+00:00"
        c.execute("INSERT INTO jobs (job, created_at) VALUES ('j', ?)", (now,))
        c.execute("INSERT INTO messages (job, agent_name, to_agent, created_at, message) "
                  "VALUES ('j', ?, ?, ?, 'old')", (self.BAD, self.BAD, now))
        c.execute("INSERT INTO agents (agent_key, name, job, joined_at, last_seen) VALUES ('k', ?, 'j', ?, ?)",
                  ("x\x1b[2J", now, now))
        c.execute("INSERT INTO transcript_images (sha256, mime, bytes, data, first_seen) "
                  "VALUES ('../../x', 'image/png', 1, x'00', ?)", (now,))

    def check_new_bad_rows_refused(self):
        import sqlite3
        c = self.h._db()
        now = "2026-01-01T00:00:00.000000+00:00"
        stmts = [
            ("INSERT INTO messages (job, agent_name, created_at, message) VALUES ('j', ?, ?, 'x')", (self.BAD, now)),
            ("INSERT INTO messages (job, agent_name, to_agent, created_at, message) VALUES ('j', 'A', ?, ?, 'x')",
             (self.BAD, now)),
            ("UPDATE messages SET agent_name = ? WHERE agent_name = 'A'", (self.BAD,)),
            ("INSERT INTO agents (agent_key, name, job, joined_at, last_seen) VALUES ('k2', ?, 'j', ?, ?)",
             (" Lisa", now, now)),
            ("UPDATE agents SET name = ? WHERE agent_key = 'k'", ("a\nb",)),
            ("INSERT INTO transcript_images (sha256, mime, bytes, data, first_seen) "
             "VALUES (?, 'image/png', 1, x'00', ?)", ("../" + "a" * 61, now)),
            ("INSERT INTO transcript_images (sha256, mime, bytes, data, first_seen) "
             "VALUES (?, 'image/png', 1, x'00', ?)", ("A" * 64, now)),
            # schema 8: memory refs are held to the same rules
            ("INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, writer, created_at) "
             "VALUES ('d', 'notes', 'j', 'k', ?, 'note-tool', ?)", (self.BAD, now)),
            ("UPDATE memory_refs SET agent_name = ? WHERE document_id = 'ok'", (self.BAD,)),
            ("INSERT INTO memory_ref_images (document_id, sha256) VALUES ('ok', ?)", ("../" + "a" * 61,)),
        ]
        c.execute("INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, writer, created_at) "
                  "VALUES ('ok', 'notes', 'j', 'k', 'Lisa', 'note-tool', ?)", (now,))
        c.execute("INSERT INTO messages (job, agent_name, created_at, message) VALUES ('j', 'A', ?, 'ok')", (now,))
        for sql, args in stmts:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                c.execute(sql, args)
        # an unrelated update of an old bad row still works (NOT VALID semantics)
        c.execute("UPDATE agents SET last_seen = ? WHERE agent_key = 'k'", (now,))
        c.execute("INSERT INTO transcript_images (sha256, mime, bytes, data, first_seen) "
                  "VALUES (?, 'image/png', 1, x'00', ?)", ("a" * 64, now))


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresSchemaUpgrade(SchemaUpgradeTests, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def setUp(self):
        super().setUp()
        self.addCleanup(self.h.reset)   # leave no bad rows behind for the next test

    def downgrade_with_bad_rows(self):
        c = self.h.conn
        for table, con in c.execute("SELECT conrelid::regclass::text, conname FROM pg_constraint "
                                    "WHERE conname LIKE 'check\\_%'").fetchall():
            c.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{con}"')
        c.execute("UPDATE board_meta SET value = '6' WHERE key = 'schema_version'")
        c.execute("INSERT INTO jobs (job) VALUES ('j')")
        c.execute("INSERT INTO messages (job, agent_name, to_agent, message) VALUES ('j', %s, %s, 'old')",
                  (self.BAD, self.BAD))
        c.execute("INSERT INTO agents (agent_key, name, job) VALUES ('k', %s, 'j')", ("x\x1b[2J",))
        c.execute("INSERT INTO transcript_images (sha256, mime, bytes, data) VALUES ('../../x', 'image/png', 1, '\\x00')")

    def check_new_bad_rows_refused(self):
        import psycopg
        c = self.h.conn
        n = c.execute("SELECT count(*) FROM pg_constraint WHERE conname LIKE 'check\\_%' "
                      "AND NOT convalidated").fetchone()[0]
        self.assertGreaterEqual(n, 4)   # added NOT VALID: the old rows didn't block the upgrade
        c.execute("INSERT INTO messages (job, agent_name, message) VALUES ('j', 'A', 'ok')")
        stmts = [
            ("INSERT INTO messages (job, agent_name, message) VALUES ('j', %s, 'x')", (self.BAD,)),
            ("INSERT INTO messages (job, agent_name, to_agent, message) VALUES ('j', 'A', %s, 'x')", (self.BAD,)),
            ("UPDATE messages SET agent_name = %s WHERE agent_name = 'A'", (self.BAD,)),
            ("INSERT INTO agents (agent_key, name, job) VALUES ('k2', %s, 'j')", (" Lisa",)),
            ("UPDATE agents SET name = %s WHERE agent_key = 'k'", ("a\nb",)),
            ("INSERT INTO transcript_images (sha256, mime, bytes, data) VALUES (%s, 'image/png', 1, '\\x00')",
             ("../" + "a" * 61,)),
            ("INSERT INTO transcript_images (sha256, mime, bytes, data) VALUES (%s, 'image/png', 1, '\\x00')",
             ("A" * 64,)),
            # schema 8: memory refs are held to the same rules
            ("INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, writer) "
             "VALUES ('d', 'notes', 'j', 'k', %s, 'note-tool')", (self.BAD,)),
            ("UPDATE memory_refs SET agent_name = %s WHERE document_id = 'ok'", (self.BAD,)),
        ]
        c.execute("INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, writer) "
                  "VALUES ('ok', 'notes', 'j', 'k', 'Lisa', 'note-tool')")
        # the old agents row with a bad name is gone (Postgres would refuse any update of it)
        self.assertIsNone(c.execute("SELECT 1 FROM agents WHERE agent_key = 'k'").fetchone())
        c.execute("INSERT INTO agents (agent_key, name, job) VALUES ('k', 'Lisa', 'j')")
        for sql, args in stmts:
            with self.subTest(sql=sql), self.assertRaises(psycopg.errors.CheckViolation):
                c.execute(sql, args)


if __name__ == "__main__":
    unittest.main()
