"""Optional Hindsight project memory, end to end against a fake Hindsight (tests/fake_hindsight.py)
and the memory board. One live smoke test runs only with SWARM_TEST_HINDSIGHT_URL set; it uses
a throwaway bank `swarm-test-<random>` and deletes it afterwards."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import unittest
from unittest import mock
import uuid

from support import posix_only  # noqa: E402
from test_hooks_cli import Env  # noqa: F401  (sets sys.path)
from fake_hindsight import FakeHindsight, dead_url  # noqa: E402

from swarm import spool  # noqa: E402
from swarm import cli as swarm  # noqa: E402

MIN = dt.timedelta(minutes=1)


def _can_listen() -> bool:
    """The fake Hindsight needs a loopback socket; some sandboxes forbid binding one."""
    import socket
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


@unittest.skipUnless(_can_listen(), "cannot bind a loopback socket here (sandbox?) for the fake Hindsight")
class HindsightEnv(Env):
    def setUp(self):
        super().setUp()
        self.fake = FakeHindsight()
        self.addCleanup(self.fake.stop)

    def enable(self, url: str | None = None, **opts) -> None:
        """Add a [hindsight] section to the config (replacing an earlier one)."""
        text = self.config.read_text().split("[hindsight]")[0]
        values = {"url": url or self.fake.url, "timeout_seconds": 1, **opts}
        lines = "".join(f"{k} = {v!r}\n".replace("'", '"') for k, v in values.items())
        self.config.write_text(text + "[hindsight]\n" + lines)
        self.cfg = swarm.load_config(self.config)

    def backdate(self, key: str, **minutes) -> None:
        """Set each timestamp field to that many minutes ago."""
        self.h.backdate_agent(key, **{field: m * MIN.total_seconds() for field, m in minutes.items()})

    def start(self, key: str = "agent-1") -> str:
        out = self.hook("start", agent_id=key)
        return self.context(out)

    def turn(self, key: str = "agent-1") -> str:
        out = self.hook("turn", agent_id=key, tool_name="Bash")
        return self.context(out) if out else ""


class MemoryOffTests(HindsightEnv):
    def test_without_url_nothing_happens_and_nothing_is_imported(self):
        sys.modules.pop("hindsight", None)
        self.cli("activate", "--job", "J")
        ctx = self.start()
        self.assertNotIn("remember", ctx)
        self.assertNotIn("[swarm memory]", ctx)
        self.backdate("agent-1", joined_at=600)
        self.assertNotIn("[swarm memory]", self.turn())
        self.assertNotIn("hindsight", sys.modules)
        self.assertEqual(self.fake.requests, [])
        rc, out, err = self.cli("remember", "--job", "J", "--as", "X", "a fact")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("[hindsight] url", err)


class MemoryTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.key_file = self.tmp / "hindsight.key"
        self.key_file.write_text("s3cret-token\n")
        self.key_file.chmod(0o600)
        self.enable(api_key_file=str(self.key_file))

    def test_bank_id_follows_the_project(self):
        from swarm import hindsight
        self.assertEqual(hindsight.bank_id("PG HA cluster/2026"), "pg-ha-cluster-2026")
        self.assertEqual(hindsight.bank_id("already_ok-1"), "already_ok-1")
        self.assertEqual(hindsight.bank_id("!!!"), "swarm")
        self.assertEqual(len(hindsight.bank_id("x" * 300)), 64)

    def test_start_recalls_the_project_memory_with_the_task(self):
        self.fake.add_memory("pg-ha", "Patroni needs full_page_writes=on for pg_rewind")
        self.fake.add_memory("pg-ha", "bare psql on db-1 hits the 5434 cluster")
        self.cli("activate", "--job", "J", "--project", "PG HA", "--task", "build the cluster")
        ctx = self.start()
        recall = self.fake.calls("POST", "/memories/recall")
        self.assertEqual(len(recall), 1)
        self.assertEqual(recall[0]["path"], "/v1/default/banks/pg-ha/memories/recall")
        self.assertEqual(recall[0]["body"]["query"], "build the cluster")
        self.assertEqual(recall[0]["auth"], "Bearer s3cret-token")
        self.assertIn('[swarm memory] what this project\'s memory knows (project "PG HA"):', ctx)
        self.assertIn("- Patroni needs full_page_writes=on for pg_rewind", ctx)
        self.assertIn("- bare psql on db-1 hits the 5434 cluster", ctx)
        self.assertIn(f"remember --job 'J' --as '{self.agent('agent-1').name}'", ctx)
        self.assertNotIn("s3cret", ctx)
        with self.board() as b:
            self.assertEqual(len(b.sync_state("agent-1").memory_seen), 2)

    def test_project_defaults_to_the_job(self):
        self.cli("activate", "--job", "My.Job")   # job names: [A-Za-z0-9._-]
        self.hook("start")
        self.assertEqual(self.fake.calls("POST", "/memories/recall")[0]["path"],
                         "/v1/default/banks/my-job/memories/recall")

    def test_recall_is_capped(self):
        self.enable(recall_max_items=3, recall_max_chars=10_000)
        for i in range(10):
            self.fake.add_memory("j", f"fact number {i}")
        self.cli("activate", "--job", "J")
        ctx = self.start()
        self.assertEqual(ctx.count("- fact number"), 3)
        self.enable(recall_max_items=50, recall_max_chars=60)
        ctx = self.context(self.hook("start", agent_id="agent-2"))
        memory = ctx[ctx.index("[swarm memory] what"):]
        self.assertEqual(memory.splitlines()[1], "- fact number 0")
        self.assertLessEqual(sum(len(l) for l in memory.splitlines()[1:]), 60)

    def test_periodic_recall_injects_only_unseen_memories(self):
        self.fake.add_memory("j", "old fact")
        self.cli("activate", "--job", "J")
        self.assertIn("old fact", self.start())
        self.assertNotIn("[swarm memory]", self.turn())
        self.assertEqual(len(self.fake.calls("POST", "/memories/recall")), 1)  # not due yet
        self.fake.add_memory("j", "new fact")
        self.backdate("agent-1", memory_recalled_at=16)
        ctx = self.turn()
        self.assertIn("[swarm memory] new memories for this project", ctx)
        self.assertIn("new fact", ctx)
        self.assertNotIn("old fact", ctx)
        self.backdate("agent-1", memory_recalled_at=16)
        self.assertNotIn("[swarm memory]", self.turn())  # recalled, nothing new: silent
        self.assertEqual(len(self.fake.calls("POST", "/memories/recall")), 3)

    def test_remember_writes_to_the_project_bank_creating_it(self):
        self.cli("activate", "--job", "J", "--project", "proj")
        self.hook("start")
        me = self.agent("agent-1").name
        rc, out, err = self.cli("remember", "--job", "J", "--as", me, "etcd", "needs  3 members")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith('remembered in project "proj" [memory swarm-'), out)
        self.assertEqual(len(self.fake.calls("PUT", "/v1/default/banks/proj")), 1)
        retain = self.fake.calls("POST", "/v1/default/banks/proj/memories")
        self.assertEqual(len(retain), 1)
        item = retain[0]["body"]["items"][0]
        self.assertEqual(item["content"], "etcd needs 3 members")
        self.assertEqual(item["tags"], ["swarm", "job:J", f"agent:{me}"])
        # plus the provenance `swarm remember` knows (host, captured_at...: test_provenance_remember)
        self.assertEqual({k: item["metadata"][k] for k in ("source", "job", "agent", "project")},
                         {"source": "swarm", "job": "J", "agent": me, "project": "proj"})
        self.assertEqual(retain[0]["auth"], "Bearer s3cret-token")
        with self.board() as b:
            self.assertIsNotNone(b.sync_state("agent-1").remembered_at)
        self.cli("remember", "--job", "J", "--as", me, "second fact")
        self.assertEqual(len(self.fake.calls("PUT", "/v1/default/banks/proj")), 1)  # exists now

    def test_nudge_once_per_quiet_stretch(self):
        self.cli("activate", "--job", "J")
        self.hook("start")
        me = self.agent("agent-1").name
        self.assertNotIn("reminder", self.turn())
        self.backdate("agent-1", joined_at=21)
        ctx = self.turn()
        self.assertIn("[swarm memory] reminder: you have stored nothing in project memory for 20 minutes", ctx)
        self.assertIn(f"remember --job 'J' --as '{me}'", ctx)
        self.assertNotIn("reminder", self.turn())
        self.backdate("agent-1", nudged_at=21)
        self.cli("remember", "--job", "J", "--as", me, "a finding")
        self.assertNotIn("reminder", self.turn())  # stored something recently
        self.backdate("agent-1", remembered_at=21)
        self.assertIn("reminder", self.turn())

    def test_remember_spools_when_the_board_is_unreachable(self):
        self.cli("activate", "--job", "J")
        self.hook("start")
        me = self.agent("agent-1").name
        self.h.set_available(False)
        rc, out, _ = self.cli("remember", "--job", "J", "--as", me, "sandboxed fact")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued (board not reachable from here"))
        self.assertEqual(len(list(self.spool_dir.glob("*.mem"))), 1)
        self.assertEqual(self.fake.calls("POST", "/memories"), [])
        self.h.set_available(True)
        self.hook("start", agent_id="courier")  # an agent hook delivers it (per-tool hooks never do)
        self.assertEqual(self.fake.banks["j"][0]["text"], "sandboxed fact")
        self.assertEqual(list(self.spool_dir.iterdir()), [])
        with self.board() as b:
            self.assertIsNotNone(b.sync_state("agent-1").remembered_at)

    def test_hindsight_down_spools_remember_and_posts_still_flow(self):
        self.cli("activate", "--job", "J")
        self.hook("start")
        me = self.agent("agent-1").name
        self.enable(url=dead_url(), retry_after_seconds=0)
        rc, out, _ = self.cli("remember", "--job", "J", "--as", me, "kept for later")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued (memory not reachable from here"))
        spool.spool_post(self.cfg, "J", "Someone", "a post", None)
        ctx = self.turn()
        self.assertIn("Someone: a post", ctx)
        self.assertEqual(len(list(self.spool_dir.glob("*.mem"))), 1)  # still waiting
        self.enable(retry_after_seconds=0)  # Hindsight is back
        self.hook("start", agent_id="courier")  # memories go out from the agent hooks only
        self.assertEqual(self.fake.banks["j"][0]["text"], "kept for later")
        self.assertEqual(list(self.spool_dir.glob("*.mem")), [])

    def test_hindsight_down_start_is_quick_and_logged(self):
        self.enable(url=dead_url())
        self.cli("activate", "--job", "J")
        ctx = self.start()
        self.assertIn("[swarm roster]", ctx)
        self.assertNotIn("[swarm memory] what", ctx)
        self.assertRegex(self.error_log.read_text(), r"memory agent-1: HindsightUnavailable: ")

    def test_slow_hindsight_is_cut_off_by_the_recall_budget(self):
        # The recall's own budget (not Hindsight being down) ends the wait, so there is no
        # unreachable marker; the join recall is retried on the next turn, bounded again.
        self.enable(timeout_seconds=0.5, recall_start_seconds=0.5, retry_after_seconds=60)
        self.fake.delay = 3
        self.cli("activate", "--job", "J")
        ctx = self.start()
        self.assertNotIn("[swarm memory] what", ctx)
        self.turn()

    def test_api_key_never_printed_on_errors(self):
        self.enable(url=dead_url(), api_key_file=str(self.key_file))
        self.cli("activate", "--job", "J")
        self.start()
        rc, out, err = self.cli("remember", "--job", "J", "--as", "Nobody", "x")
        self.assertNotIn("s3cret", out + err + self.error_log.read_text())


class ColdRecallTests(HindsightEnv):
    """A slow Hindsight answers a recall late: the join recall waits for it, a mid-work one doesn't."""

    def test_start_recall_waits_for_a_slow_cold_recall(self):
        from swarm import hooks
        self.enable(timeout_seconds=1, recall_start_seconds=5)   # per-call timeout is raised to the budget
        self.fake.add_memory("j", "cold fact")
        self.fake.delays[("POST", "/memories/recall")] = hooks.HOOK_RECALL_SECONDS + 0.8   # beyond the old cap
        self.cli("activate", "--job", "J")
        self.assertIn("cold fact", self.start())

    def test_env_overrides_the_config(self):
        from swarm import hooks
        cfg = {"hindsight": {"recall_start_seconds": 7}}
        self.assertEqual(hooks._start_recall_seconds(cfg), 7.0)
        with mock.patch.dict(os.environ, {hooks.HOOK_RECALL_ENV: "3"}):
            self.assertEqual(hooks._start_recall_seconds(cfg), 3.0)
        with mock.patch.dict(os.environ, {hooks.HOOK_RECALL_ENV: "junk"}):
            self.assertEqual(hooks._start_recall_seconds(cfg), 7.0)
        self.assertEqual(hooks._start_recall_seconds({"hindsight": {}}), 6.0)
        self.assertEqual(hooks._start_recall_seconds({"hindsight": {"recall_start_seconds": 60}}),
                         hooks.HOOK_RECALL_MAX_SECONDS)

    def test_start_recall_that_runs_out_of_time_is_retried_on_the_next_turn(self):
        self.enable(timeout_seconds=10, recall_start_seconds=0.6)
        self.fake.add_memory("j", "late fact")
        self.fake.delays[("POST", "/memories/recall")] = 1.5
        self.cli("activate", "--job", "J")
        self.assertNotIn("late fact", self.start())
        self.assertRegex(self.error_log.read_text(), r"memory agent-1: HindsightOutOfTime: ")
        self.fake.delays.clear()                                     # warm now; no recall_minutes wait
        self.assertIn("late fact", self.turn())


class FailureScopeTests(HindsightEnv):
    """Only a Hindsight that can't be reached trips the global circuit breaker; an error one bank
    or one item answers with stays with that bank or item, and keeps the server's detail."""

    def setUp(self):
        super().setUp()
        self.enable(retry_after_seconds=60)
        from swarm import hindsight
        self.hs = hindsight
        self.client = hindsight.Client(self.cfg)
        # host-only: not in the sandbox-writable spool
        self.marker = self.home_dir() / ".local/share/swarm/host/hindsight-unreachable"

    def home_dir(self):
        from pathlib import Path
        return Path(os.environ["HOME"])

    def trip(self):
        self.fake.fail(503, "upstream down")
        with self.assertRaises(self.hs.HindsightUnavailable):
            self.client.recall("any", "q")

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_unreachable_marker_not_in_spool(self):
        self.trip()
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.spool_dir / ".hindsight-unreachable").exists())

    def test_unreachable_marker_symlink_not_followed(self):
        victim = self.home_dir() / "victim"
        self.marker.parent.mkdir(parents=True, mode=0o700)
        self.marker.symlink_to(victim)
        self.trip()
        self.assertFalse(victim.exists())

    def test_a_marker_planted_in_the_spool_is_ignored(self):
        # a sandboxed agent could write one there to switch memory off for everyone
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        (self.spool_dir / ".hindsight-unreachable").touch()
        self.fake.add_memory("fine", "healthy bank fact")
        self.assertEqual([i["text"] for i in self.client.recall("fine", "q")], ["healthy bank fact"])

    def test_a_bank_5xx_does_not_mark_hindsight_unreachable(self):
        self.fake.fail(500, "bank broken is corrupt", bank="broken", method="PUT")
        with self.assertRaises(self.hs.HindsightError) as cm:
            self.client.ensure_bank("broken")
        self.assertNotIsInstance(cm.exception, self.hs.HindsightUnavailable)
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(cm.exception.detail, "bank broken is corrupt")
        self.assertIn("bank broken is corrupt", str(cm.exception))
        self.assertFalse(self.marker.exists())
        self.fake.add_memory("fine", "healthy bank fact")
        self.assertEqual([i["text"] for i in self.client.recall("fine", "q")], ["healthy bank fact"])

    def test_a_bank_5xx_on_remember_queues_it_and_recall_still_works(self):
        self.fake.fail(500, "boom", bank="broken")
        self.fake.add_memory("other", "other job fact")
        self.cli("activate", "--job", "broken")
        rc, out, _ = self.cli("remember", "--job", "broken", "--as", "X", "a fact")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued (memory refused it for now: HTTP 500"), out)
        self.assertIn("boom", out)
        self.assertEqual(len(list(self.spool_dir.glob("*.mem"))), 1)
        self.assertFalse(self.marker.exists())
        self.cli("deactivate", "--job", "broken")
        self.cli("activate", "--job", "other")
        self.assertIn("other job fact", self.context(self.hook("start", agent_id="agent-9")))

    def test_gateway_errors_trip_the_breaker(self):
        for code in (502, 503, 504):
            self.marker.unlink(missing_ok=True)
            self.fake.heal()
            self.fake.fail(code, "upstream down")
            with self.assertRaises(self.hs.HindsightUnavailable):
                self.client.recall("any", "q")
            self.assertTrue(self.marker.exists(), code)
            n = len(self.fake.requests)
            with self.assertRaises(self.hs.HindsightUnavailable):
                self.client.recall("other", "q")
            self.assertEqual(len(self.fake.requests), n)  # skipped: marked down

    def test_4xx_keeps_the_detail_and_does_not_trip_the_breaker(self):
        self.fake.fail(422, [{"loc": ["body", "items"], "msg": "field required"}], bank="b", method="POST")
        self.fake.banks["b"] = []
        with self.assertRaises(self.hs.HindsightError) as cm:
            self.client.retain("b", "x", tags=[], metadata={})
        self.assertEqual(cm.exception.status, 422)
        self.assertIn("field required", str(cm.exception))
        self.assertFalse(self.marker.exists())
        self.cli("activate", "--job", "b")
        rc, out, err = self.cli("remember", "--job", "b", "--as", "X", "a fact")
        self.assertEqual(rc, 1)
        self.assertIn("memory refused it: HTTP 422", err)
        self.assertIn("field required", err)


class SpoolRetryTests(HindsightEnv):
    """Spooled memories: no head-of-line blocking, a failure is scoped to its bank or item and
    recorded on the file, and after 24 hours of failing it is parked as `.stuck` (retryable)."""

    def setUp(self):
        super().setUp()
        self.enable(retry_after_seconds=60)
        self.cli("activate", "--job", "J")
        self.hook("start")

    def spool_mem(self, text: str, project: str) -> None:
        spool.spool_memory(self.cfg, "J", "Someone", text, project)
        time.sleep(0.01)  # distinct mtimes: oldest first

    def records(self, suffix: str = ".mem") -> list[dict]:
        return [json.loads(f.read_text()) for f in sorted(self.spool_dir.glob("*" + suffix))]

    def flush(self) -> int:
        with self.board() as b:
            return spool.flush_spool(b, self.cfg)

    def warnings(self) -> list:
        with self.board() as b:
            return [m for m in b.recent_messages(100, job="J") if m.agent_name == "swarm"]

    def age_failure(self, hours: float) -> None:
        [f] = self.spool_dir.glob("*.mem")
        rec = json.loads(f.read_text())
        rec["first_failed"] -= hours * 3600
        f.write_text(json.dumps(rec))

    def test_a_failing_bank_does_not_block_memories_for_healthy_banks(self):
        self.fake.fail(500, "bank broken is corrupt", bank="broken")
        self.spool_mem("for the broken bank", "broken")
        self.spool_mem("for the healthy bank", "healthy")
        self.assertEqual(self.flush(), 1)
        self.assertEqual(self.fake.banks["healthy"][0]["text"], "for the healthy bank")
        [rec] = self.records()
        self.assertEqual(rec["text"], "for the broken bank")
        self.assertEqual(rec["attempts"], 1)
        self.assertAlmostEqual(rec["first_failed"], time.time(), delta=5)
        self.assertIn("HTTP 500", rec["last_error"])
        self.assertIn("bank broken is corrupt", rec["last_error"])
        self.assertEqual(list(self.spool_dir.glob("*.bad")), [])
        self.assertFalse((self.spool_dir / ".hindsight-unreachable").exists())

    def test_a_bank_error_skips_that_banks_other_memories_for_this_flush_only(self):
        self.fake.fail(500, "nope", bank="broken", method="PUT")
        for i in range(3):
            self.spool_mem(f"broken fact {i}", "broken")
        self.spool_mem("healthy fact", "healthy")
        self.assertEqual(self.flush(), 1)
        self.assertEqual(len(self.fake.calls("PUT", "/banks/broken")), 1)
        self.assertEqual(len(self.records()), 3)
        self.assertEqual([r.get("attempts") for r in self.records()].count(1), 1)

    def test_an_item_rejected_with_4xx_is_kept_and_its_bank_mates_are_delivered(self):
        self.fake.fail(422, "content has a NUL byte", content="poison")
        self.spool_mem("poison pill", "shared")
        self.spool_mem("good fact", "shared")
        self.assertEqual(self.flush(), 1)
        self.assertEqual([m["text"] for m in self.fake.banks["shared"]], ["good fact"])
        [rec] = self.records()
        self.assertEqual((rec["text"], rec["attempts"]), ("poison pill", 1))
        self.assertIn("content has a NUL byte", rec["last_error"])
        self.assertEqual(list(self.spool_dir.glob("*.bad")), [])

    def test_a_failed_memory_waits_retry_after_seconds_before_the_next_attempt(self):
        self.fake.fail(500, "nope", bank="broken")
        self.spool_mem("fact", "broken")
        self.flush()
        n = len(self.fake.requests)
        self.flush()
        self.assertEqual(len(self.fake.requests), n)  # backing off
        self.enable(retry_after_seconds=0)
        self.flush()
        self.assertGreater(len(self.fake.requests), n)
        [rec] = self.records()
        self.assertEqual(rec["attempts"], 2)
        self.fake.heal()
        self.assertEqual(self.flush(), 1)
        self.assertEqual(self.records(), [])

    def test_an_outage_is_not_counted_against_the_memories(self):
        self.spool_mem("fact", "some")
        self.enable(url=dead_url(), retry_after_seconds=0)
        self.flush()
        [rec] = self.records()
        self.assertNotIn("attempts", rec)

    def test_after_24h_of_failing_it_is_parked_as_stuck_with_one_warning(self):
        self.fake.fail(500, "bank broken is corrupt", bank="broken")
        self.spool_mem("fact", "broken")
        self.enable(retry_after_seconds=0)
        self.flush()
        self.age_failure(23)
        self.flush()
        self.assertEqual(len(self.records()), 1)  # 23 hours: still retried
        self.age_failure(1.01)
        self.flush()
        self.assertEqual(list(self.spool_dir.glob("*.mem")), [])
        self.assertEqual(list(self.spool_dir.glob("*.bad")), [])
        [stuck] = self.records(".stuck")
        self.assertEqual((stuck["text"], stuck["attempts"]), ("fact", 3))
        [warning] = self.warnings()
        self.assertIn("24h", warning.message)
        self.assertIn("bank broken is corrupt", warning.message)
        self.assertIn("swarm spool retry", warning.message)
        n = len(self.fake.requests)
        self.flush()
        self.flush()
        self.assertEqual(len(self.fake.requests), n)  # the flusher leaves .stuck alone
        self.assertEqual(len(self.warnings()), 1)

    def test_spool_retry_requeues_stuck_memories(self):
        self.fake.fail(500, "nope", bank="broken")
        self.spool_mem("fact", "broken")
        self.enable(retry_after_seconds=0)
        self.flush()
        self.age_failure(25)
        self.flush()
        self.assertEqual(len(self.records(".stuck")), 1)
        rc, out, err = self.cli("spool", "retry")
        self.assertEqual((rc, out, err), (0, "requeued 1 stuck memory\n", ""))
        self.assertEqual(list(self.spool_dir.glob("*.stuck")), [])
        [rec] = self.records()
        self.assertEqual(rec["attempts"], 2)       # history kept
        self.assertNotIn("first_failed", rec)      # a fresh 24 hours
        self.fake.heal()
        self.hook("start", agent_id="courier")  # memories go out from the agent hooks only
        self.assertEqual(self.fake.banks["broken"][0]["text"], "fact")
        self.assertEqual(self.records(), [])
        rc, out, _ = self.cli("spool", "retry")
        self.assertEqual((rc, out), (0, "requeued 0 stuck memories\n"))

    def test_memory_switched_off_is_kept_not_thrown_away(self):
        self.spool_mem("fact", "some")
        self.cfg["hindsight"]["url"] = ""
        self.flush()
        self.assertEqual(list(self.spool_dir.glob("*.bad")), [])
        [rec] = self.records()
        self.assertEqual(rec["attempts"], 1)


@unittest.skipUnless(_can_listen(), "cannot bind a loopback socket here (sandbox?) for the fake Hindsight")
class HindsightApiShapesTest(HindsightEnv):
    """The client against both API shapes: 0.8.6 (GET /profile answers, /config is 200 for any
    bank) and 0.10.x (/profile is 410 Gone, /config 404s a missing bank)."""

    def client_for(self, api: str):
        from swarm import hindsight
        self.fake.api = api
        self.enable()
        return hindsight.Client(self.cfg)

    def test_bank_exists_and_ensure_bank_on_both_shapes(self):
        for api in ("0.8", "0.10"):
            with self.subTest(api=api):
                self.fake.banks.clear()
                self.fake.requests.clear()
                client = self.client_for(api)
                self.assertFalse(client.bank_exists("fresh"))
                client.ensure_bank("fresh")
                self.assertTrue(client.bank_exists("fresh"))
                client.ensure_bank("fresh")   # never PUTs an existing bank
                self.assertEqual(len(self.fake.calls("PUT", "/v1/default/banks/fresh")), 1)

    def test_a_410_on_profile_is_asked_once_per_client(self):
        client = self.client_for("0.10")
        client.ensure_bank("a")
        client.ensure_bank("b")
        self.assertEqual(len(self.fake.calls("GET", "/a/profile") + self.fake.calls("GET", "/b/profile")), 1)
        self.assertEqual(len(self.fake.calls("GET", "/config")), 2)

    def test_0_8_never_asks_config(self):
        client = self.client_for("0.8")
        client.ensure_bank("a")
        self.assertEqual(self.fake.calls("GET", "/config"), [])

    def test_retain_and_recall_on_both_shapes(self):
        for api in ("0.8", "0.10"):
            with self.subTest(api=api):
                client = self.client_for(api)
                self.assertEqual(client.recall("absent-" + api, "q"), [])   # 404 -> []
                client.retain("bank-" + api, "a fact", tags=["swarm"], metadata={"source": "swarm"},
                              document_id="d1")
                self.assertEqual([i["text"] for i in client.recall("bank-" + api, "q")], ["a fact"])
                self.assertEqual(client.document("bank-" + api, "d1")["id"], "d1")
                self.assertIsNone(client.document("bank-" + api, "nope"))
                client.delete_bank("bank-" + api)

    def test_other_profile_errors_are_not_mistaken_for_410(self):
        client = self.client_for("0.10")
        self.fake.fail(500, "boom", method="GET")
        from swarm import hindsight
        with self.assertRaises(hindsight.HindsightError) as cm:
            client.bank_exists("x")
        self.assertEqual(cm.exception.status, 500)

    def test_remember_creates_the_bank_on_0_10(self):
        self.fake.api = "0.10"
        self.enable()
        self.cli("activate", "--job", "J", "--project", "proj")
        self.hook("start")
        me = self.agent("agent-1").name
        rc, out, err = self.cli("remember", "--job", "J", "--as", me, "a fact")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(self.fake.calls("PUT", "/v1/default/banks/proj")), 1)
        self.assertEqual(len(self.fake.calls("POST", "/v1/default/banks/proj/memories")), 1)


@unittest.skipUnless(os.environ.get("SWARM_TEST_HINDSIGHT_URL"),
                     "set SWARM_TEST_HINDSIGHT_URL to run the live Hindsight smoke test")
class LiveHindsightSmokeTest(unittest.TestCase):
    """Writes ONLY to a new throwaway bank swarm-test-<random>, deleted afterwards."""

    def test_retain_recall_roundtrip_in_a_throwaway_bank(self):
        from swarm import hindsight
        from support import base_config
        cfg = base_config()
        cfg["hindsight"].update(url=os.environ["SWARM_TEST_HINDSIGHT_URL"], timeout_seconds=30,
                                api_key_file=os.environ.get("SWARM_TEST_HINDSIGHT_KEY_FILE", ""))
        cfg["board"]["spool_dir"] = os.environ.get("TMPDIR", "/tmp") + "/swarm-live-test"
        bank = f"swarm-test-{uuid.uuid4().hex[:10]}"
        self.assertTrue(bank.startswith("swarm-test-"))
        client = hindsight.Client(cfg)
        self.assertFalse(client.bank_exists(bank))  # brand new: we never touch an existing bank
        try:
            client.retain(bank, "The swarm live smoke test stores this fact about zebras.",
                          tags=["swarm", "job:live-test"], metadata={"source": "swarm"})
            deadline = time.monotonic() + 120
            found = []
            while time.monotonic() < deadline and not found:
                found = [i for i in client.recall(bank, "zebras") if "zebra" in i["text"].lower()]
                time.sleep(3)
            self.assertTrue(found, "retained fact never became recallable")
        finally:
            client.delete_bank(bank)


if __name__ == "__main__":
    unittest.main()
